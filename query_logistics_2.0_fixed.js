// ═══════════════════════════════════════════════════════════════════
//  菜鸟物流查询 — @字段名版（无需列号，永不 Field Not Exist）
// ═══════════════════════════════════════════════════════════════════

// ─── 配置区 ─────────────────────────────────────
var API_URL       = "https://cainiao-server.onrender.com/query";

var SHEET_NAME    = "";    // 留空则自动使用当前活动表

// 【表头名称】改成你表格中的实际表头文字
var H_MAIL_NO  = "物流单号";   // 必填
var H_STATUS   = "物流状态";   // 必填
var H_STATUS1  = "物流状态1";   // 写入"|"前面的状态文字（如"妥投"、"运输中"）
var H_TIME     = "更新时间";   // 留空=不写入该列
var H_DELIVER  = "签收日期";   // 留空=不写入该列

// 已签收关键词（符合任一即跳过查询）
var FINAL_STATUSES = ["妥投", "成功签收"];

var BATCH_SIZE = 20;                // 每批20单
var BETWEEN_BATCH_DELAY_MS = 800;
var MAX_RUN_TIME_MS = 4.5 * 60 * 1000;
var SUB_BATCH_SIZE = 3;             // 每次API查询最多3单（防超时空响应）

// ─── 工具函数 ───────────────────────────────────
function pad2(n) { return n < 10 ? "0" + n : "" + n; }
function fmtDate(d) {
    return d.getFullYear() + "/" + pad2(d.getMonth()+1) + "/" + pad2(d.getDate())
         + " " + pad2(d.getHours()) + ":" + pad2(d.getMinutes());
}
function sleep(ms) { var t = new Date().getTime(); while (new Date().getTime() - t < ms) {} }
function safeStr(v) { return (v === null || v === undefined) ? "" : String(v).trim(); }

// ─── API 查询 ──────────────────────
function queryApi(mailNo) {
    var url = API_URL + "?mailNo=" + encodeURIComponent(mailNo);
    try {
        var resp = HTTP.get(url);
        return JSON.parse(resp.text());
    } catch(e) {
        return { code: -1, error: safeStr(e.message || e).substring(0, 120) };
    }
}

// ─── 主函数 ─────────────────────────────────────
function 查询物流() {
    var startTime = new Date();
    console.log("══════ 物流查询开始 @ " + fmtDate(startTime) + " ══════");

    // ─── 获取多维表 ───
    var sheets = Application.Sheet.GetSheets();
    if (!sheets || !sheets.length) { console.log("❌ 无法获取多维表"); return; }
    var sheetId = sheets[0].id;
    console.log("📋 多维表 ID: " + sheetId);

    // ─── 校验字段是否存在 ───
    var fieldArr = Application.Field.GetFields({ SheetId: sheetId });
    var fieldMap = {};
    for (var fi = 0; fi < fieldArr.length; fi++) fieldMap[fieldArr[fi].name] = true;
    var missing = [];
    if (!fieldMap[H_MAIL_NO]) missing.push(H_MAIL_NO);
    if (!fieldMap[H_STATUS])  missing.push(H_STATUS);
    if (missing.length > 0) { console.log("❌ 缺少字段：" + missing.join("、")); return; }

    // ─── 读取全部记录 ───
    var result = Application.Record.GetRecords({ SheetId: sheetId });
    var records = result.records;
    var total = records.length;
    if (total === 0) { console.log("📭 多维表为空"); return; }
    console.log("📦 共 " + total + " 条记录");

    // ─── 智能分流 ───
    var toProcess = [], skipped = 0;
    for (var i = 0; i < total; i++) {
        var rec = records[i];
        var no = (rec.fields ? rec.fields[H_MAIL_NO] : null) || "";
        no = safeStr(no);
        if (!no) continue;
        var status = (rec.fields ? rec.fields[H_STATUS] : null) || "";
        status = safeStr(status);
        var isFinal = false;
        for (var fi = 0; fi < FINAL_STATUSES.length; fi++) {
            if (status.indexOf(FINAL_STATUSES[fi]) >= 0) { isFinal = true; break; }
        }
        if (isFinal) { skipped++; continue; }
        toProcess.push({ rec: rec, mailNo: no, hasStatus: !!status });
    }

    if (toProcess.length === 0) {
        console.log("📭 没有需要查询的单号（全部已签收）");
        return;
    }

    // 智能分流：有空状态的只查空状态，否则全查
    var hasEmpty = false;
    for (var i = 0; i < toProcess.length; i++) {
        if (!toProcess[i].hasStatus) { hasEmpty = true; break; }
    }
    if (hasEmpty) {
        toProcess = toProcess.filter(function(r) { return !r.hasStatus; });
        console.log("🔍 智能分流：只查询 " + toProcess.length + " 条空状态单号");
    } else {
        console.log("🔍 智能分流：全部重查 " + toProcess.length + " 条");
    }

    // ─── 批量查询 ───
    var ok = 0, errCount = 0;
    var isTimeOut = false;
    var nowStr = fmtDate(new Date());

    for (var offset = 0; offset < toProcess.length; offset += BATCH_SIZE) {
        if (new Date().getTime() - startTime.getTime() > MAX_RUN_TIME_MS) {
            isTimeOut = true;
            console.log("\n⚠️ 运行时间已达4.5分钟，为防止系统报错已安全中止！");
            break;
        }

        var batch = toProcess.slice(offset, Math.min(offset + BATCH_SIZE, toProcess.length));

        // 先批量写入"查询中..."
        var pendingRecords = [];
        for (var bi = 0; bi < batch.length; bi++) {
            var fields = {};
            fields[H_STATUS] = "查询中...";
            if (H_STATUS1) fields[H_STATUS1] = "查询中...";
            pendingRecords.push({ id: batch[bi].rec.id, fields: fields });
        }
        try { Application.Record.UpdateRecords({ SheetId: sheetId, Records: pendingRecords }); } catch(e) {}

        // 收集单号，按 SUB_BATCH_SIZE 拆子批（每批最多3单，防服务器超时空响应）
        var mailNoBatch = [];
        for (var bi = 0; bi < batch.length; bi++) { mailNoBatch.push(batch[bi].mailNo); }

        var resultMap = {};
        for (var subIdx = 0; subIdx < mailNoBatch.length; subIdx += SUB_BATCH_SIZE) {
            var subBatch = mailNoBatch.slice(subIdx, Math.min(subIdx + SUB_BATCH_SIZE, mailNoBatch.length));
            var ret = queryApi(subBatch.join(","));
            if (ret && ret.code === 0 && ret.data) {
                for (var di = 0; di < ret.data.length; di++) {
                    var d = ret.data[di];
                    if (d && d.mailNo) resultMap[String(d.mailNo).trim()] = d;
                }
            } else {
                var errMsg = (ret && ret.error) ? ret.error.substring(0, 80) : "API返回异常";
                console.log("  ⚠️ API查询失败 (" + subBatch.length + "单): " + errMsg);
            }
            sleep(300); // 子批间延时，防服务器限流
        }

        // 构建写入数据（每个记录独立 fields 对象）
        var updateRecords = [];
        for (var bi = 0; bi < batch.length; bi++) {
            var mailNo = batch[bi].mailNo;
            var d = resultMap[mailNo];
            var fields = {};

            if (d) {
                var label = d.status ? String(d.status).trim() : "未知";
                var eventText = d.latestEvent || "";
                var timeText = d.latestTime || "";
                var statusText = eventText ? label + " | " + eventText : label;
                var finalTime = timeText ? timeText.replace(/-/g, "/") : nowStr;

                var hasError = d.error && d.error.length > 0;
                if (!hasError || label.indexOf("失败") < 0) {
                    // 正常查到数据
                    fields[H_STATUS] = statusText;
                    if (H_STATUS1) fields[H_STATUS1] = label;
                    if (H_TIME)    fields[H_TIME] = finalTime;
                    if (H_DELIVER) {
                        var isFinalDv = (label.indexOf("妥投") >= 0 || label.indexOf("已签收") >= 0 || label.indexOf("签收") >= 0 || statusText.indexOf("退回") >= 0);
                        fields[H_DELIVER] = isFinalDv ? finalTime : "";
                    }
                    console.log("  ✅ " + mailNo + " → " + label);
                    ok++;
                } else {
                    fields[H_STATUS] = label;
                    if (H_STATUS1) fields[H_STATUS1] = label;
                    if (H_TIME)    fields[H_TIME] = "";
                    if (H_DELIVER) fields[H_DELIVER] = "";
                    console.log("  ⚠️ " + mailNo + " → " + label + " (" + String(d.error).substring(0, 40) + ")");
                    errCount++;
                }
            } else {
                fields[H_STATUS] = "查询失败: 无返回数据";
                if (H_STATUS1) fields[H_STATUS1] = "查询失败: 无返回数据";
                if (H_TIME)    fields[H_TIME] = "";
                if (H_DELIVER) fields[H_DELIVER] = "";
                console.log("  ❌ " + mailNo + " → 无返回数据");
                errCount++;
            }

            updateRecords.push({ id: batch[bi].rec.id, fields: fields });
        }

        // 批量写入
        try {
            Application.Record.UpdateRecords({ SheetId: sheetId, Records: updateRecords });
        } catch(e) {
            console.log("  ⚠️ 批量写入失败，尝试逐条写入...");
            for (var ui = 0; ui < updateRecords.length; ui++) {
                try {
                    Application.Record.UpdateRecords({ SheetId: sheetId, Records: [updateRecords[ui]] });
                } catch(e2) {
                    console.log("  ⚠️ 第" + (ui+1) + "条写入失败: " + safeStr(e2).substring(0, 60));
                }
            }
        }

        if (offset + BATCH_SIZE < toProcess.length) { sleep(BETWEEN_BATCH_DELAY_MS); }
    }

    var elapsed = Math.round((new Date().getTime() - startTime.getTime()) / 1000);
    var msg = "\n══════ 完成 @ " + fmtDate(new Date()) + " ══════\n"
            + "  ✅ 成功: " + ok + "\n  ❌ 失败: " + errCount + "\n  ⏭️ 跳过(已签收): " + skipped + "\n  ⏱ 耗时: " + elapsed + " 秒";
    if (isTimeOut) { msg = "⚠️ 达到5分钟运行上限已暂停，请再次点击运行继续！\n" + msg; }
    console.log(msg);
    try { MsgBox(msg, 0); } catch(e) { try { Alert(msg); } catch(e2) {} }
}

查询物流();
