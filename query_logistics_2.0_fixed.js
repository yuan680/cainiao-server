// ═══════════════════════════════════════════════════════════════════
//  菜鸟物流查询 — RecordRange 扫描版（无上限）
// ═══════════════════════════════════════════════════════════════════

// ─── 配置区 ─────────────────────────────────────
var API_URL = "https://cainiao-server.onrender.com/query";

// ★ 工作表名：留空自动检测当前活动表格，有多个表格时指定名称
var SHEET_NAME = "";

// ★ 总列数：WPS 多维表不提供安全探测列数的原生方法
// （RecordRange 超出范围会触发引擎级崩溃，try/catch 无法拦截）
// 请在此按实际列数填写，如果以后增删了列记得修改此项
var TABLE_COLS = 13;

// ★ 列索引（1-based）—— 留空(0)表示自动探测
var COL_MAIL_NO  = 0;   // 物流单号（自动探测带LP前缀的列）
var COL_STATUS   = 5;   // 物流状态
var COL_STATUS1  = 6;   // 物流状态1
var COL_TIME     = 7;   // 更新时间
var COL_DELIVER  = 8;   // 签收日期

// ★ 表头名称（自动匹配列序号时使用）
var HEADER_NAMES = {
    mailNo:   "物流单号",
    status:   "物流状态",
    status1:  "物流状态1",
    time:     "更新时间",
    deliver:  "签收日期"
};

// 已签收关键词（符合任一即跳过查询）
var FINAL_STATUSES = ["妥投", "成功签收"];

var BATCH_SIZE = 100;           // 每组处理行数（WPS写入批量，与SUB_BATCH_SIZE独立）
var SUB_BATCH_SIZE = 15;         // ★ 每个HTTP请求查询几个单号（增至15，让服务端利用4线程并行处理，大幅减少总请求数）
var BETWEEN_REQUESTS_MS = 800;  // 请求间延迟基准值（大分批每批~6s，延迟保持1.2-1.6s防限流）
var MAX_RUN_TIME_MS = 4.5 * 60 * 1000;

// ─── 工具函数 ───────────────────────────────────
function pad2(n) { return n < 10 ? "0" + n : "" + n; }
function fmtDate(d) {
    return d.getFullYear() + "/" + pad2(d.getMonth()+1) + "/" + pad2(d.getDate())
         + " " + pad2(d.getHours()) + ":" + pad2(d.getMinutes());
}
function sleep(ms) { var t = new Date().getTime(); while (new Date().getTime() - t < ms) {} }
function safeStr(v) { return (v === null || v === undefined) ? "" : String(v).trim(); }

// ─── 全局请求计数器（所有子批间共享，用于自适应限速）───
var _gReqCount = 0;

// 子批间延迟（基准1200ms + 随机抖动 400~800ms，打散请求节奏防限流）
// 每累计 50 次请求额外增加 20% 延迟（自适应）
function sleepBetweenSubBatches() {
    _gReqCount++;
    var baseMs = BETWEEN_REQUESTS_MS;
    // 累计请求越多，基准延迟逐渐增加（最多翻倍）
    if (_gReqCount > 100) { baseMs = Math.round(BETWEEN_REQUESTS_MS * 1.5); }
    else if (_gReqCount > 50) { baseMs = Math.round(BETWEEN_REQUESTS_MS * 1.2); }
    var jitter = Math.floor(Math.random() * 400) + 400; // 400~800ms
    sleep(baseMs + jitter);
}

// ─── API 查询（批量，最多100单号/请求）───
// 返回 { resultMap, rateLimited } — rateLimited 标记是否触发了限流
function queryMailNos(mailNos) {
    var resultMap = {};
    var rateLimited = false;
    var url = API_URL + "?mailNo=" + encodeURIComponent(mailNos.join(","));
    for (var r = 0; r < 5; r++) {
        try {
            var resp = HTTP.get(url);  // 不使用 timeout 参数（WPS AirScript 不支持 options 对象）
            if (resp) {
                if (resp.status === 200) {
                    var text = resp.text();
                    if (text && text !== "") {
                        var parsed = JSON.parse(text);
                        if (parsed && parsed.code === 0 && parsed.data) {
                            var gotCount = 0;
                            for (var di = 0; di < parsed.data.length; di++) {
                                var d = parsed.data[di];
                                if (d && d.mailNo) {
                                    resultMap[String(d.mailNo).trim()] = d;
                                    gotCount++;
                                }
                            }
                            // 检查是否疑似限流：返回结果远少于请求数
                            var expected = mailNos.length;
                            if (expected > 1 && gotCount < Math.max(1, Math.ceil(expected * 0.3))) {
                                rateLimited = true;
                                var backoff = 2000 * (r + 1) + Math.floor(Math.random() * 1000);  // 2s/4s/6s/8s/10s + jitter
                                console.log("  [限流] 请求" + expected + "个单号，仅收到" + gotCount + "条结果，疑似限流，" + backoff + "ms后重试 (" + (r+1) + "/5)");
                                resultMap = {};
                                sleep(backoff);
                                continue;
                            }
                            return { resultMap: resultMap, rateLimited: false }; // 成功，直接返回
                        } else if (parsed && parsed.code !== 0) {
                            // 服务器返回业务错误码
                            var backoff = 2000 * (r + 1);
                            console.log("  [业务错误] code=" + parsed.code + " msg=" + (parsed.message || "").substring(0, 50) + "，" + backoff + "ms后重试 (" + (r+1) + "/5)");
                            sleep(backoff);
                            continue;
                        }
                    }
                } else {
                    console.log("  [诊断] 状态码=" + resp.status + " mailNo=" + mailNos[0].substring(0, 16) + "...");
                    if (resp.status === 429) {
                        rateLimited = true;
                        var rlBackoff = 3000 * (r + 1) + Math.floor(Math.random() * 2000);  // 3s/6s/9s/12s/15s + jitter
                        console.log("  [限流] 429，" + rlBackoff + "ms后重试 (" + (r+1) + "/5)");
                        sleep(rlBackoff);
                        continue;
                    }
                    if (resp.status === 502 || resp.status === 503) {
                        var backoff = (r + 1) * 3000;
                        console.log("  [诊断] " + resp.status + "，等待" + backoff + "ms后退 (" + (r+1) + "/5)");
                        sleep(backoff);
                        continue;
                    }
                }
            }
        } catch(e) {
            console.log("  [诊断] 请求异常: " + safeStr(e.message || e).substring(0, 80));
            sleep(2000);
        }
    }
    return { resultMap: resultMap, rateLimited: rateLimited };
}

// ─── 预热服务器（反复查询直到成功响应）───
function warmupServer() {
    console.log("🔥 预热服务器（等待冷启动，最多60秒）...");
    var testNos = ["LP00813174920057"];
    for (var w = 0; w < 20; w++) {
        try {
            var url = API_URL + "?mailNo=" + testNos[0];
            var resp = HTTP.get(url);  // 预热也使用无 options 调用
            if (resp && resp.status === 200) {
                var text = resp.text();
                if (text && text !== "" && text.indexOf("code") >= 0) {
                    console.log("⚡ 预热完成（第" + (w + 1) + "次尝试成功）");
                    return true;
                }
            }
        } catch(e) {}
        console.log("  ⏳ 等待服务器响应... (" + (w + 1) + "/20)");
        sleep(1000);
    }
    console.log("⚠️ 预热超时，继续尝试查询...");
    return false;
}

// ─── 查询一批（内部按 SUB_BATCH_SIZE 分批请求）───
// 返回 { resultMap, rateLimited }
function queryBatch(mailNos) {
    var resultMap = {};
    var anyRateLimited = false;
    for (var offset = 0; offset < mailNos.length; offset += SUB_BATCH_SIZE) {
        var sub = mailNos.slice(offset, Math.min(offset + SUB_BATCH_SIZE, mailNos.length));
        var subResult = queryMailNos(sub);
        for (var key in subResult.resultMap) {
            if (subResult.resultMap.hasOwnProperty(key)) resultMap[key] = subResult.resultMap[key];
        }
        if (subResult.rateLimited) {
            anyRateLimited = true;
            // 如果连续限流，额外多等一会儿
            var extraPause = 2000 + Math.floor(Math.random() * 2000);
            console.log("  [限流防御] 额外等待 " + extraPause + "ms 降低上游压力");
            sleep(extraPause);
        }
        // 子批间延迟（带 jitter 防限流）
        if (offset + SUB_BATCH_SIZE < mailNos.length) {
            sleepBetweenSubBatches();
        }
    }
    return { resultMap: resultMap, rateLimited: anyRateLimited };
}

// ─── 主函数 ─────────────────────────────────────
function 查询物流() {
    var startTime = new Date();
    console.log("══════ 物流查询开始 @ " + fmtDate(startTime) + " ══════");
    try {

    // ─── 获取工作表 & 自动检测列序号 ───
    var sh = null;
    try {
        var activeSheet = Application.ActiveSheet;
        if (activeSheet && activeSheet.name) {
            sh = Application.Sheets(activeSheet.name);
        }
    } catch(e) {}
    if (!sh) {
        try {
            var sheets = Application.Sheet.GetSheets();
            if (sheets && sheets.length) {
                sh = Application.Sheets(sheets[0].name);
            }
        } catch(e) {}
    }
    if (!sh) { console.log("❌ 无法获取工作表"); return; }

    // ─── 扫描第1行探测有效列数 ───
    // 从第1列开始扫描，读空值即判断已达有效列尾
    // 上限 TABLE_COLS 是安全阀，防止引擎崩溃
    var totalCols = 0;
    for (var c = 1; c <= TABLE_COLS; c++) {
        var cell = null;
        try { cell = sh.RecordRange(1, c); } catch(e) { break; }
        if (cell === null || cell === undefined) break;
        var v = String(cell.Value).trim();
        if (v === "") { totalCols = c - 1; break; }
        totalCols = c;
    }
    if (totalCols < 1) totalCols = TABLE_COLS;
    console.log("📋 有效列数: " + totalCols + " (上限 " + TABLE_COLS + ")");

    // ─── 智能列探测：扫描第1行，根据内容自动匹配列 ───
    var detected = { mailNo: COL_MAIL_NO, status: COL_STATUS, status1: COL_STATUS1,
                     time: COL_TIME, deliver: COL_DELIVER };
    var colInfo = [];
    for (var c = 1; c <= totalCols; c++) {
        var cell = sh.RecordRange(1, c);
        if (cell === null || cell === undefined) continue;
        var v = String(cell.Value).trim();
        colInfo.push("[" + c + "]" + v.substring(0, 30));

        if (!detected.mailNo && /^LP/i.test(v)) { detected.mailNo = c; }
        if (!detected.status && /退回|签收|运输|派送|揽收/.test(v)) { detected.status = c; }
    }
    console.log("📋 第1行各列: " + colInfo.join(" | "));
    console.log("📋 探测结果：物流单号列=" + detected.mailNo + " 状态列=" + detected.status
              + " 状态1=" + detected.status1 + " 时间=" + detected.time + " 签收日期=" + detected.deliver);

    if (!detected.mailNo) { console.log("❌ 未找到物流单号列（第1行没有LP开头的值）"); return; }
    // 将探测结果写回配置变量（后续代码用这些变量名）
    COL_MAIL_NO  = detected.mailNo;
    COL_STATUS   = detected.status  || COL_STATUS;
    COL_STATUS1  = detected.status1 || COL_STATUS1;
    COL_TIME     = detected.time    || COL_TIME;
    COL_DELIVER  = detected.deliver || COL_DELIVER;

    // 缓存 RecordRange 读写函数
    var gv = function(row, col) {
        try {
            var cell = sh.RecordRange(row, col);
            if (cell === null || cell === undefined) return "";
            var v = cell.Value;
            return (v === null || v === undefined) ? "" : String(v).trim();
        } catch(e) { return ""; }
    };
    var sv = function(row, col, val) {
        try { sh.RecordRange(row, col).Value = val; } catch(e) {}
    };

    // ─── 扫描数据行（逐行扫描 + 空行检测，不用 RecordRange("?",1)）───
    var toProcess = [], skipped = 0;
    var emptyCount = 0;
    var totalScanned = 0;
    var MAX_SCAN_ROWS = 5000;
    var MAX_EMPTY_ROWS = 5;
    console.log("📦 扫描数据行…");

    for (var r = 1; r <= MAX_SCAN_ROWS; r++) {
        if ((r - 1) % 200 === 0) {
            console.log("  📍 扫描第 " + r + " 行…（空行计数 " + emptyCount + "）");
        }
        var no = gv(r, COL_MAIL_NO);
        if (!no) {
            emptyCount++;
            if (emptyCount >= MAX_EMPTY_ROWS) {
                console.log("📦 扫描结束（连续" + MAX_EMPTY_ROWS + "行空白），共扫描 " + totalScanned + " 行数据");
                break;
            }
            continue;
        }
        emptyCount = 0;
        totalScanned++;
        var status = gv(r, COL_STATUS);
        var isFinal = false;
        for (var fi = 0; fi < FINAL_STATUSES.length; fi++) {
            if (status.indexOf(FINAL_STATUSES[fi]) >= 0) { isFinal = true; break; }
        }
        if (isFinal) { skipped++; continue; }
        toProcess.push({ row: r, mailNo: no, hasStatus: !!status });
    }

    if (toProcess.length === 0 && skipped === 0) {
        console.log("📭 没有找到物流单号");
        return;
    }
    if (toProcess.length === 0 && skipped > 0) {
        console.log("📭 所有单号均已签收，无需查询");
        return;
    }
    console.log("📊 待查询 " + toProcess.length + " 单，跳过(已签收) " + skipped + " 单");

    // 智能分流
    var hasEmpty = false;
    for (var i = 0; i < toProcess.length; i++) {
        if (!toProcess[i].hasStatus) { hasEmpty = true; break; }
    }
    if (hasEmpty) {
        toProcess = toProcess.filter(function(rr) { return !rr.hasStatus; });
        console.log("🔍 智能分流：只查询 " + toProcess.length + " 条空状态单号");
    } else {
        console.log("🔍 智能分流：全部重查 " + toProcess.length + " 条");
    }

    // ─── 预热服务器 ───
    warmupServer();

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

        // 先写入"查询中..."
        for (var bi = 0; bi < batch.length; bi++) {
            sv(batch[bi].row, COL_STATUS, "查询中...");
            if (COL_STATUS1) sv(batch[bi].row, COL_STATUS1, "查询中...");
            if (COL_DELIVER) sv(batch[bi].row, COL_DELIVER, "");
        }

        // 查询批次内所有单号（内部按 SUB_BATCH_SIZE 分组请求）
        var mailNoBatch = [];
        for (var bi = 0; bi < batch.length; bi++) { mailNoBatch.push(batch[bi].mailNo); }
        console.log("  📡 查询 " + mailNoBatch.length + " 单（每" + SUB_BATCH_SIZE + "单/请求）...");

        var batchResult = queryBatch(mailNoBatch);
        var resultMap = batchResult.resultMap;
        var batchRateLimited = batchResult.rateLimited;

        // 逐行写入结果
        for (var bi = 0; bi < batch.length; bi++) {
            var mailNo = batch[bi].mailNo;
            var d = resultMap[mailNo];
            var row = batch[bi].row;

            if (d) {
                var label = d.status ? String(d.status).trim() : "未知";
                var eventText = d.latestEvent || "";
                var timeText = d.latestTime || "";
                var statusText = eventText ? label + " | " + eventText : label;
                var finalTime = timeText ? timeText.replace(/-/g, "/") : nowStr;

                var hasError = d.error && d.error.length > 0;
                if (!hasError || label.indexOf("失败") < 0) {
                    sv(row, COL_STATUS, statusText);
                    if (COL_STATUS1) sv(row, COL_STATUS1, label);
                    if (COL_TIME)    sv(row, COL_TIME, finalTime);
                    if (COL_DELIVER) {
                        var isFinalDv = (label.indexOf("妥投") >= 0 || label.indexOf("已签收") >= 0 || label.indexOf("签收") >= 0 || statusText.indexOf("退回") >= 0);
                        sv(row, COL_DELIVER, isFinalDv ? finalTime : "");
                    }
                    console.log("  ✅ " + mailNo + " → " + label);
                    ok++;
                } else {
                    sv(row, COL_STATUS, label);
                    if (COL_STATUS1) sv(row, COL_STATUS1, label);
                    if (COL_TIME)    sv(row, COL_TIME, "");
                    if (COL_DELIVER) sv(row, COL_DELIVER, "");
                    console.log("  ⚠️ " + mailNo + " → " + label + " (" + String(d.error).substring(0, 40) + ")");
                    errCount++;
                }
            } else {
                sv(row, COL_STATUS, "查询失败: 无返回数据");
                if (COL_STATUS1) sv(row, COL_STATUS1, "查询失败: 无返回数据");
                if (COL_TIME)    sv(row, COL_TIME, "");
                if (COL_DELIVER) sv(row, COL_DELIVER, "");
                console.log("  ❌ " + mailNo + " → 无返回数据");
                errCount++;
            }
        }

        // 批间延迟，防止服务器过载
        // 如果本批触发了限流，额外等待更长时间
        var batchDelay = batchRateLimited ? (BETWEEN_REQUESTS_MS * 3 + Math.floor(Math.random() * 2000))
                                         : (BETWEEN_REQUESTS_MS + Math.floor(Math.random() * 500));
        sleep(batchDelay);
    }

    var elapsed = Math.round((new Date().getTime() - startTime.getTime()) / 1000);
    var msg = "\n══════ 完成 @ " + fmtDate(new Date()) + " ══════\n"
            + "  ✅ 成功: " + ok + "\n  ❌ 失败: " + errCount + "\n  ⏭️ 跳过(已签收): " + skipped + "\n  ⏱ 耗时: " + elapsed + " 秒";
    if (isTimeOut) { msg = "⚠️ 达到5分钟运行上限已暂停，请再次点击运行继续！\n" + msg; }
    console.log(msg);
    try { MsgBox(msg, 0); } catch(e) { try { Alert(msg); } catch(e2) {} }
    } catch(e) {
        var errMsg = "❌ 脚本错误: " + safeStr(e.message || e).substring(0, 200);
        console.log(errMsg);
        try { MsgBox(errMsg, 0); } catch(e2) { try { Alert(errMsg); } catch(e3) {} }
    }
}

查询物流();
