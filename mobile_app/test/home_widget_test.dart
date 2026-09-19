// 首页验收测试（对应任务书验收标准）：
//  ① 首页核心动作只有"拍照/选图上传"，不再有任何字段编辑表单；
//  ② 上传后展示一句话摘要（红/黄/绿 + 一句关键问题 + 批次编号），非完整明细；
//  ③ 现场速查：输入编号能查到历史批次的等级与核心结论；
//  ④ 弱网暂存横幅：失败照片有清晰提示（自动重试），不阻塞继续拍照。
//
// 网络用假ApiClient注入；队列为内存模式（持久化行为在 queue_test 用真实IO覆盖）；
// SharedPreferences用mock——widget测试运行在FakeAsync区，真实文件IO不会完成。
import 'dart:io';

import 'package:ceb_verifier/main.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

class FakeApi extends ApiClient {
  FakeApi({this.failWithNetwork = false}) : super('http://fake');

  final bool failWithNetwork;
  int quickCheckCalls = 0;
  List<String> lookupQueries = [];

  @override
  Future<QuickCheckResult> quickCheck(List<QueuedPhoto> photos,
      Future<List<int>> Function(QueuedPhoto) readBytes) async {
    quickCheckCalls++;
    if (failWithNetwork) {
      throw SocketException('network unreachable');
    }
    return QuickCheckResult.fromJson({
      'batch_id': 'MB-20260920-150000-DEMO',
      'risk_level': 'red',
      'risk_grade': 'high',
      'risk_label': '高风险',
      'risk_score': 88,
      'one_line': '必需单证齐全性检查：缺少必需单证：装箱单、铁路运单',
      'doc_count': 2,
      'created_at': '2026-09-20T15:00:00',
      'detail_hint': '请在电脑端核验网页输入批次编号 MB-20260920-150000-DEMO 查看完整报告',
    });
  }

  @override
  Future<List<LookupMatch>> lookup(String query) async {
    lookupQueries.add(query);
    if (query.toUpperCase().contains('SMU')) {
      return [
        LookupMatch.fromJson({
          'batch_id': 'MB-20260919-090000-11AA',
          'created_at': '2026-09-19T09:00:00',
          'risk_level': 'yellow',
          'risk_label': '中风险',
          'one_line': '毛重一致性（容差1%）：毛重超出1%容差',
          'doc_count': 5,
        })
      ];
    }
    return [];
  }
}

Future<void> _pumpHome(WidgetTester tester,
    {required FakeApi api,
    UploadQueue? queue,
    Future<String?> Function()? scannerBuilder}) async {
  SharedPreferences.setMockInitialValues({});
  await tester.pumpWidget(MaterialApp(
      home: HomePage(
          apiFactory: (_) => api, queue: queue, scannerBuilder: scannerBuilder)));
  await tester.pumpAndSettle();
  await tester.pumpAndSettle(); // 给启动补传的异步链留出完成空间
}

void main() {
  testWidgets('首页只有拍照主线，没有任何字段编辑表单', (tester) async {
    await _pumpHome(tester, api: FakeApi(), queue: UploadQueue.memory());
    expect(find.text('拍 照 上 传'), findsOneWidget);
    expect(find.text('从相册选择'), findsOneWidget);
    expect(find.text('现场速查'), findsOneWidget);
    // 移除项：不允许再出现编辑/核验交互
    expect(find.text('保存并返回'), findsNothing);
    expect(find.text('开始核验'), findsNothing);
    expect(find.text('编辑字段'), findsNothing);
  });

  testWidgets('上传成功展示一句话摘要横幅（等级+问题+批次编号），无完整明细', (tester) async {
    final api = FakeApi();
    final queue = UploadQueue.memory();
    await queue.enqueue('photo1.jpg', List<int>.generate(64, (i) => i));
    await _pumpHome(tester, api: api, queue: queue);
    // 启动时发现暂存照片自动补传成功 → 横幅出现一句话反馈
    expect(api.quickCheckCalls, 1);
    expect(queue.hasPending, isFalse);
    expect(find.text('高风险'), findsOneWidget);
    expect(find.textContaining('缺少必需单证'), findsOneWidget);
    expect(find.textContaining('批次编号 MB-20260920-150000-DEMO'), findsOneWidget);
    expect(find.textContaining('电脑端'), findsWidgets);
    // 不应有完整明细的痕迹
    expect(find.text('核验明细'), findsNothing);
    expect(find.text('分数构成（可解释分解）'), findsNothing);
    expect(find.text('AI 修正建议'), findsNothing);
  });

  testWidgets('现场速查：输入运单号查到历史结论；查无记录有明确提示', (tester) async {
    final api = FakeApi();
    await _pumpHome(tester, api: api, queue: UploadQueue.memory());
    await tester.enterText(find.byType(TextField), 'SMU/T/2026-09');
    await tester.tap(find.text('查询'));
    await tester.pumpAndSettle();
    expect(api.lookupQueries, contains('SMU/T/2026-09'));
    expect(find.textContaining('毛重超出1%容差'), findsOneWidget);
    expect(find.textContaining('MB-20260919-090000-11AA'), findsOneWidget);

    // 查无记录
    await tester.enterText(find.byType(TextField), 'NO-SUCH-999');
    await tester.tap(find.text('查询'));
    await tester.pumpAndSettle();
    expect(find.textContaining('未查询到历史核验记录'), findsOneWidget);
  });

  testWidgets('弱网失败：照片暂存并出现自动重试横幅，不丢失也不卡死', (tester) async {
    final api = FakeApi(failWithNetwork: true);
    final queue = UploadQueue.memory();
    await queue.enqueue('photo1.jpg', List<int>.generate(64, (i) => i));
    await _pumpHome(tester, api: api, queue: queue);
    expect(api.quickCheckCalls, 1);
    expect(queue.hasPending, isTrue, reason: '失败照片必须保留');
    expect(find.textContaining('等待上传（网络恢复后自动重试）'), findsOneWidget);
    // 拍照按钮仍然可用（现场不被卡住，可以继续拍下一张）
    expect(
        tester
            .widget<FilledButton>(
                find.widgetWithText(FilledButton, '拍 照 上 传'))
            .onPressed,
        isNotNull);
  });

  // ---------------- 扫码速查（P1补齐：扫码作为查询输入方式之一） ----------------

  /// 完整走一遍扫码入口：点扫码按钮 → 首次使用说明弹窗 → 开始扫码 →
  /// 注入的假扫码器返回结果。
  Future<FakeApi> runScanFlow(WidgetTester tester, String? scannedValue) async {
    final api = FakeApi();
    await _pumpHome(tester, api: api, queue: UploadQueue.memory(),
        scannerBuilder: () async => scannedValue);
    await tester.tap(find.byTooltip('扫码查询'));
    await tester.pumpAndSettle();
    expect(find.text('扫码速查'), findsOneWidget, reason: '首次使用必须有中文用途说明');
    await tester.tap(find.text('开始扫码'));
    await tester.pumpAndSettle();
    return api;
  }

  testWidgets('扫码识别成功：自动填入输入框并立即查询，无需再点查询按钮', (tester) async {
    final api = await runScanFlow(tester, 'SMU/T/2026-09');
    expect(api.lookupQueries, contains('SMU/T/2026-09'),
        reason: '扫码成功必须自动触发查询');
    expect(find.text('SMU/T/2026-09'), findsOneWidget,
        reason: '识别出的编号应自动填入输入框');
    expect(find.textContaining('毛重超出1%容差'), findsOneWidget,
        reason: '查询结果直接展示');
  });

  testWidgets('扫码到无关内容：清晰提示"未识别到有效编号"，不发起查询', (tester) async {
    final api = await runScanFlow(tester, 'https://example.com/promo?x=1');
    expect(api.lookupQueries, isEmpty, reason: '无效内容不浪费一次查询请求');
    expect(find.textContaining('未识别到有效的单证编号'), findsWidgets);
    // 手动输入路径依然可用
    await tester.enterText(find.byType(TextField), 'SMU/T/2026-09');
    await tester.tap(find.text('查询'));
    await tester.pumpAndSettle();
    expect(api.lookupQueries, contains('SMU/T/2026-09'));
  });

  testWidgets('取消扫码/权限拒绝返回：静默回到手动输入，无报错无查询', (tester) async {
    final api = await runScanFlow(tester, null);
    expect(api.lookupQueries, isEmpty);
    expect(find.textContaining('未识别到有效的单证编号'), findsNothing,
        reason: '用户主动取消不应报错');
    // 手动输入查询依然可用（拒绝摄像头权限后的降级路径）
    await tester.enterText(find.byType(TextField), 'MB-20260919-090000-11AA');
    await tester.tap(find.text('查询'));
    await tester.pumpAndSettle();
    expect(api.lookupQueries, contains('MB-20260919-090000-11AA'));
  });
}
