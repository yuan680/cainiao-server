// ═══════════════════════════════════════════════
//  WPS 多维表格 GetRecords API 诊断脚本
//  在 WPS 中打开「多维表格」→「新建脚本」→ 粘贴运行
//  输出会显示在「控制台」和弹窗
// ═══════════════════════════════════════════════

function _popup(msg) {
    console.log("[弹窗] " + msg);
    try { MsgBox(msg, 0); return; } catch(e) {}
    try { WPS.MsgBox(msg, 0); return; } catch(e) {}
    try { Application.Sheet.MsgBox(msg, 0); return; } catch(e) {}
    try { Alert(msg); return; } catch(e) {}
}

try {
    // 1. 获取当前表格
    var sheets = Application.Sheet.GetSheets();
    if (!sheets || !sheets.length) throw "获取表格失败";
    var sheetId = null;
    try {
        var activeSheet = Application.ActiveSheet;
        if (activeSheet && activeSheet.id) sheetId = activeSheet.id;
    } catch(_ae) {}
    if (!sheetId) sheetId = sheets[0].id;
    console.log("[测试] sheetId=" + sheetId);

    // 2. 测试 1: Page=1, PageSize=500
    console.log("\n========== 测试1: Page=1, PageSize=500 ==========");
    var r1 = Application.Record.GetRecords({ SheetId: sheetId, PageSize: 500, Page: 1 });
    console.log("返回=" + JSON.stringify({ hasResp: !!r1, hasRecords: !!(r1 && r1.records), count: (r1 && r1.records) ? r1.records.length : 0, offset: (r1 ? r1.offset : undefined), hasOffset: !!(r1 && r1.offset) }));
    if (r1 && r1.records && r1.records.length > 0) {
        console.log("第1条 id=" + r1.records[0].id + " 单号=" + ((r1.records[0].fields || {})["物流单号"] || ""));
        console.log("第2条 id=" + r1.records[1].id + " 单号=" + ((r1.records[1].fields || {})["物流单号"] || ""));
        console.log("第3条 id=" + r1.records[2].id + " 单号=" + ((r1.records[2].fields || {})["物流单号"] || ""));
        if (r1.records.length > 3) {
            console.log("第4条 id=" + r1.records[3].id + " 单号=" + ((r1.records[3].fields || {})["物流单号"] || ""));
            console.log("最后1条 id=" + r1.records[r1.records.length-1].id + " 单号=" + ((r1.records[r1.records.length-1].fields || {})["物流单号"] || ""));
        }
    }

    // 3. 测试 2: 如果 support offset，用 offset 参数请求下一页
    if (r1 && r1.offset) {
        console.log("\n========== 测试2: 使用 offset=" + JSON.stringify(r1.offset) + " ==========");
        var r2 = Application.Record.GetRecords({ SheetId: sheetId, PageSize: 500, offset: r1.offset });
        console.log("返回=" + JSON.stringify({ hasResp: !!r2, hasRecords: !!(r2 && r2.records), count: (r2 && r2.records) ? r2.records.length : 0, offset: (r2 ? r2.offset : undefined) }));
        if (r2 && r2.records && r2.records.length > 0) {
            console.log("第1条 id=" + r2.records[0].id + " 单号=" + ((r2.records[0].fields || {})["物流单号"] || ""));
            console.log("最后1条 id=" + r2.records[r2.records.length-1].id + " 单号=" + ((r2.records[r2.records.length-1].fields || {})["物流单号"] || ""));
            // 判断是否和第1页数据重复
            if (r2.records[0].id === r1.records[0].id) {
                console.log("⚠️ 结果: offset 无效！返回的数据和第1页相同（id=" + r2.records[0].id + "）");
            } else {
                console.log("✅ 结果: offset 有效！返回了不同的数据");
            }
        } else {
            console.log("结果: offset 返回空，说明无更多数据");
        }

        // 测试 2b: 尝试用 offset + Page 组合
        console.log("\n========== 测试2b: offset=WY + Page=2 ==========");
        var r2b = Application.Record.GetRecords({ SheetId: sheetId, PageSize: 500, Page: 2, offset: r1.offset });
        console.log("返回数=" + ((r2b && r2b.records) ? r2b.records.length : 0) + " offset=" + (r2b ? r2b.offset : "无"));
        if (r2b && r2b.records && r2b.records.length > 0) {
            console.log("第1条 id=" + r2b.records[0].id);
            if (r2b.records[0].id === r1.records[0].id) {
                console.log("⚠️ 和Page1重复");
            } else {
                console.log("✅ 新数据");
            }
        }
    }

    // 4. 测试 3: 试试不同 PageSize（200/100/50）看分页是否正常
    console.log("\n========== 测试3: PageSize=200 + Page=1,2,3 ==========");
    for (var psi = 1; psi <= 3; psi++) {
        try {
            var r3p = Application.Record.GetRecords({ SheetId: sheetId, PageSize: 200, Page: psi });
            var c3 = (r3p && r3p.records) ? r3p.records.length : 0;
            var id3 = c3 > 0 ? r3p.records[0].id : "N/A";
            var dup3 = (psi > 1 && id3 === "FT") ? " ⚠️重复" : (c3 > 0 ? " ✅" : "");
            console.log("  PageSize=200 Page=" + psi + " 返回" + c3 + "条 首id=" + id3 + dup3);
        } catch(_e3) { console.log("  PageSize=200 Page=" + psi + " 错误: " + (_e3.message || _e3)); }
    }
    console.log("\n========== 测试3b: PageSize=100 + Page=1~5 ==========");
    for (var psi = 1; psi <= 5; psi++) {
        try {
            var r3b = Application.Record.GetRecords({ SheetId: sheetId, PageSize: 100, Page: psi });
            var c3b = (r3b && r3b.records) ? r3b.records.length : 0;
            var id3b = c3b > 0 ? r3b.records[0].id : "N/A";
            var dup3b = (psi > 1 && id3b === (r1.records[0].id || "FT")) ? " ⚠️重复" : (c3b > 0 ? " ✅" : "");
            console.log("  PageSize=100 Page=" + psi + " 返回" + c3b + "条 首id=" + id3b + dup3b);
            if (c3b > 0 && dup3b.indexOf("✅") >= 0 && psi === 1) {
                // 记录第一条id用作后续判断
            }
        } catch(_e3b) { console.log("  PageSize=100 Page=" + psi + " 错误: " + (_e3b.message || _e3b)); }
    }
    console.log("\n========== 测试3c: PageSize=50 + Page=1~5 ==========");
    for (var psi = 1; psi <= 5; psi++) {
        try {
            var r3c = Application.Record.GetRecords({ SheetId: sheetId, PageSize: 50, Page: psi });
            var c3c = (r3c && r3c.records) ? r3c.records.length : 0;
            if (c3c > 0) {
                var fId = r3c.records[0].id;
                var lId = r3c.records[c3c-1].id;
                var dup = (psi > 1 && fId === r1.records[0].id) ? " ⚠️重复" : " ✅";
                console.log("  PageSize=50 Page=" + psi + " 返回" + c3c + "条 范围=" + fId + "~" + lId + dup);
            } else {
                console.log("  PageSize=50 Page=" + psi + " 空数据");
            }
        } catch(_e3c) { console.log("  PageSize=50 Page=" + psi + " 错误: " + (_e3c.message || _e3c)); }
    }

    // 5. 测试 4: Page=1~5 重复确认
    console.log("\n========== 测试4: Page=1~5 (PageSize=500) 快速确认 ==========");
    try {
        for (var pi = 1; pi <= 5; pi++) {
            var rp = Application.Record.GetRecords({ SheetId: sheetId, PageSize: 500, Page: pi });
            var cnt = (rp && rp.records) ? rp.records.length : 0;
            var off = rp ? rp.offset : "无";
            var firstId = cnt > 0 ? rp.records[0].id : "N/A";
            var isDup = (pi > 1 && firstId === r1.records[0].id) ? " ⚠️重复" : (cnt > 0 ? " ✅" : "");
            console.log("  Page=" + pi + " 返回" + cnt + "条 offset=" + off + " 首id=" + firstId + isDup);
            if (cnt === 0) break;
        }
    } catch(_e4) { console.log("  测试4错误: " + (_e4.message || _e4)); }

    // 6. 测试 5: 读取物流单号列的值
    console.log("\n========== 测试5: Page1 物流单号汇总 ==========");
    try {
        if (r1 && r1.records) {
            var lpCount = 0;
            var totalCount = r1.records.length;
            var mailNos = [];
            for (var mi = 0; mi < r1.records.length; mi++) {
                var no = ((r1.records[mi].fields || {})["物流单号"] || "").toString().trim();
                if (no) mailNos.push(no);
                if (no.indexOf("LP") === 0) lpCount++;
            }
            console.log("Page1数据: 共" + totalCount + "行, LP开头的=" + lpCount + "行");
            console.log("前5个单号: " + mailNos.slice(0,5).join(", "));
            console.log("后5个单号: " + mailNos.slice(-5).join(", "));
        }
    } catch(_e5) { console.log("  测试5错误: " + (_e5.message || _e5)); }

    // 7. 测试6: 试试 QueryRecords（如果存在）
    console.log("\n========== 测试6: 尝试 QueryRecords ==========");
    try {
        if (Application.Record.QueryRecords) {
            var r6 = Application.Record.QueryRecords({ SheetId: sheetId, Limit: 500 });
            console.log("QueryRecords返回=" + JSON.stringify({ count: (r6 && r6.records) ? r6.records.length : 0, offset: (r6 ? r6.offset : undefined) }));
        } else {
            console.log("QueryRecords 方法不存在");
        }
    } catch(_e6) { console.log("  QueryRecords错误: " + (_e6.message || _e6)); }

    // 8. 测试7: PageSize=2000（看看能否超过500）
    console.log("\n========== 测试7: PageSize=2000 (Page=1) ==========");
    try {
        var r7 = Application.Record.GetRecords({ SheetId: sheetId, PageSize: 2000, Page: 1 });
        console.log("返回=" + JSON.stringify({ count: (r7 && r7.records) ? r7.records.length : 0, offset: (r7 ? r7.offset : undefined) }));
        if (r7 && r7.records && r7.records.length > 0) {
            console.log("首id=" + r7.records[0].id + " 尾id=" + r7.records[r7.records.length - 1].id);
            var allNos = [];
            for (var mi = 0; mi < r7.records.length; mi++) {
                allNos.push(((r7.records[mi].fields || {})["物流单号"] || "").toString());
            }
            console.log("单号数=" + allNos.length + " 前5=" + allNos.slice(0,5).join(",") + " 后5=" + allNos.slice(-5).join(","));
            console.log("与Page1首行同一行? " + (r7.records[0].id === r1.records[0].id ? "⚠️ 重复" : "✅ 新数据"));
        }
    } catch(_e7) { console.log("  测试7错误: " + (_e7.message || _e7)); }

    // 9. 测试8: PageSize=3000（更大值）
    console.log("\n========== 测试8: PageSize=3000 (Page=1) ==========");
    try {
        var r8 = Application.Record.GetRecords({ SheetId: sheetId, PageSize: 3000, Page: 1 });
        console.log("返回=" + JSON.stringify({ count: (r8 && r8.records) ? r8.records.length : 0 }));
    } catch(_e8) { console.log("  测试8错误: " + (_e8.message || _e8)); }

    console.log("\n========== 诊断完成 ==========");
    _popup("API诊断完成，控制台查看结果");

} catch(e) {
    _popup("错误: " + (e.message || e));
    console.log("[错误] " + JSON.stringify({msg: e.message || e.toString(), stack: e.stack || ""}));
}
