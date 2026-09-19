// 操作流程截图（golden渲染）：用真实Flutter渲染管线产出"拍照→上传→反馈→速查"
// 链路截图，汇报与验收用。网络用假ApiClient（截图中数据与后端契约字段一致），
// 字体加载本机真实中文字体（等线），保证截图文字为真实字形而非测试占位块。
//
// 重新生成：flutter test test/flow_screenshots_test.dart --update-goldens
import 'dart:convert';
import 'dart:io';
import 'dart:typed_data';

import 'package:ceb_verifier/main.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

class ScreenshotApi extends ApiClient {
  ScreenshotApi({this.failWithNetwork = false}) : super('http://demo');

  final bool failWithNetwork;
  int calls = 0;

  @override
  Future<QuickCheckResult> quickCheck(List<QueuedPhoto> photos,
      Future<List<int>> Function(QueuedPhoto) readBytes) async {
    calls++;
    if (failWithNetwork) throw SocketException('network unreachable');
    return QuickCheckResult.fromJson({
      'batch_id': 'MB-20260920-143001-8A3C',
      'created_at': '2026-09-20T14:30:01',
      'risk_grade': 'high',
      'risk_level': 'red',
      'risk_label': '高风险',
      'risk_score': 88,
      'one_line': '必需单证齐全性检查：缺少必需单证：装箱单、铁路运单',
      'doc_count': 4,
      'detail_hint': '请在电脑端核验网页输入批次编号 MB-20260920-143001-8A3C 查看完整报告',
    });
  }

  @override
  Future<List<LookupMatch>> lookup(String query) async {
    if (query.toUpperCase().contains('SMU')) {
      return [
        LookupMatch.fromJson({
          'batch_id': 'MB-20260919-090000-11AA',
          'created_at': '2026-09-19T09:00:00',
          'risk_level': 'yellow',
          'risk_label': '中风险',
          'one_line': '毛重一致性（容差1%）：毛重超出1%容差，装箱单（13550kg）偏离基准12300kg',
          'doc_count': 5,
        }),
        LookupMatch.fromJson({
          'batch_id': 'MB-20260912-101500-77B2',
          'created_at': '2026-09-12T10:15:00',
          'risk_level': 'green',
          'risk_label': '低风险',
          'one_line': '全部检查项通过，未发现问题',
          'doc_count': 5,
        }),
      ];
    }
    return [];
  }
}

/// 加载本机真实字体：'Roboto'挂中文字体（等线）让中文渲染成真实字形；
/// 'MaterialIcons'挂图标字体，避免图标变占位块。
/// 必须在 setUpAll（FakeAsync区外）加载：testWidgets内用runAsync会
/// 干扰后续的FakeAsync微任务链（启动补传链不推进）。
Future<void> _loadFonts() async {
  const candidates = [
    'C:/Windows/Fonts/Deng.ttf',
    'C:/Windows/Fonts/simhei.ttf',
    'C:/Windows/Fonts/msyh.ttc',
  ];
  for (final path in candidates) {
    final f = File(path);
    if (await f.exists()) {
      final bytes = await f.readAsBytes();
      final loader = FontLoader('Roboto')
        ..addFont(Future.value(
            ByteData.view(bytes.buffer, bytes.offsetInBytes, bytes.lengthInBytes)));
      await loader.load();
      break;
    }
  }
  const iconPath =
      'C:/Users/Jeff0/.zcode/workspace/default/mobile_build_env/flutter_sdk/'
      'bin/cache/artifacts/material_fonts/materialicons-regular.otf';
  final iconFile = File(iconPath);
  if (await iconFile.exists()) {
    final bytes = await iconFile.readAsBytes();
    final loader = FontLoader('MaterialIcons')
      ..addFont(Future.value(
          ByteData.view(bytes.buffer, bytes.offsetInBytes, bytes.lengthInBytes)));
    await loader.load();
  }
}

void _usePhoneViewport(WidgetTester tester) {
  tester.view.physicalSize = const Size(1080, 2160);
  tester.view.devicePixelRatio = 3.0;
  addTearDown(tester.view.reset);
}

Future<void> _seedHistory() async {
  // 与App真实存储口径一致：'recent_results'是jsonEncode后的字符串
  Map<String, Object> entry(String batchId, String level, String grade,
          String label, int score, String oneLine, int count, String createdAt) =>
      {
        'batch_id': batchId,
        'risk_level': level, 'risk_grade': grade, 'risk_label': label,
        'risk_score': score, 'one_line': oneLine, 'doc_count': count,
        'created_at': createdAt,
        'detail_hint': '请在电脑端核验网页输入批次编号 $batchId 查看完整报告',
      };
  final history = jsonEncode([
    entry('MB-20260919-161200-4471', 'green', 'low', '低风险', 0,
        '全部检查项通过，未发现问题', 5, '2026-09-19T16:12:00'),
    entry('MB-20260918-090540-2C90', 'yellow', 'medium', '中风险', 35,
        '货物描述一致性：存在表述差异，处于语义灰色区', 3, '2026-09-18T09:05:40'),
  ]);
  SharedPreferences.setMockInitialValues({'recent_results': history});
}

Future<void> main() async {
  await TestWidgetsFlutterBinding.ensureInitialized();
  await _loadFonts();
  testWidgets('截图01 · 首页：拍照即传主线 + 现场速查入口', (tester) async {
    _usePhoneViewport(tester);
    await _seedHistory();
    await tester.pumpWidget(MaterialApp(
        debugShowCheckedModeBanner: false,
        home: HomePage(
            apiFactory: (_) => ScreenshotApi(), queue: UploadQueue.memory())));
    await tester.pumpAndSettle();
    await expectLater(find.byType(MaterialApp),
        matchesGoldenFile('goldens_flow/01_home_capture.png'));
  });

  testWidgets('截图02 · 弱网：照片暂存，网络恢复后自动重试', (tester) async {
    _usePhoneViewport(tester);
    await _seedHistory();
    final queue = UploadQueue.memory();
    await queue.enqueue('IMG_0930.jpg', List<int>.generate(64, (i) => i));
    await queue.enqueue('IMG_0931.jpg', List<int>.generate(64, (i) => i * 3));
    await tester.pumpWidget(MaterialApp(
        debugShowCheckedModeBanner: false,
        home: HomePage(
            apiFactory: (_) => ScreenshotApi(failWithNetwork: true),
            queue: queue)));
    await tester.pumpAndSettle();
    await expectLater(find.byType(MaterialApp),
        matchesGoldenFile('goldens_flow/02_offline_pending.png'));
  });

  testWidgets('截图03 · 一句话反馈：红/黄/绿 + 一句关键问题 + 批次编号', (tester) async {
    _usePhoneViewport(tester);
    await _seedHistory();
    final queue = UploadQueue.memory();
    await queue.enqueue('IMG_0930.jpg', List<int>.generate(64, (i) => i));
    await tester.pumpWidget(MaterialApp(
        debugShowCheckedModeBanner: false,
        home: HomePage(
            apiFactory: (_) => ScreenshotApi(), queue: queue)));
    await tester.pumpAndSettle();
    await expectLater(find.byType(MaterialApp),
        matchesGoldenFile('goldens_flow/03_quick_feedback.png'));
  });

  testWidgets('截图04 · 现场速查：输入运单号 → 历史核验结论', (tester) async {
    _usePhoneViewport(tester);
    await _seedHistory();
    await tester.pumpWidget(MaterialApp(
        debugShowCheckedModeBanner: false,
        home: HomePage(
            apiFactory: (_) => ScreenshotApi(), queue: UploadQueue.memory())));
    await tester.pumpAndSettle();
    await tester.enterText(find.byType(TextField), 'SMU/T/2026-09');
    await tester.tap(find.text('查询'));
    await tester.pumpAndSettle();
    await expectLater(find.byType(MaterialApp),
        matchesGoldenFile('goldens_flow/04_field_lookup.png'));
  });
}
