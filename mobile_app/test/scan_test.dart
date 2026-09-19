// 扫码速查测试：扫码是查询输入方式的补充（验收三种场景——识别成功/无效内容/权限拒绝）。
// 摄像头无法在测试环境打开，因此：
//   - 扫码文本校验、错误降级UI用纯函数/纯widget直接测；
//   - 端到端接线（扫码→自动填入→立即查询）在 home_widget_test 用注入的假扫码器测。
import 'package:ceb_verifier/main.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:mobile_scanner/mobile_scanner.dart';

void main() {
  group('扫码文本校验（sanitizeScannedCode）', () {
    test('合法单证编号：运单号/箱号/批次编号/报关单号（含首尾空白容忍）', () {
      expect(sanitizeScannedCode('SMU/T/2026-09'), 'SMU/T/2026-09');
      expect(sanitizeScannedCode('MB-20260920-143001-8A3C'),
          'MB-20260920-143001-8A3C');
      expect(sanitizeScannedCode('MSKU8765432'), 'MSKU8765432');
      expect(sanitizeScannedCode('29152026000118888'), '29152026000118888');
      expect(sanitizeScannedCode('  INV-APP-2026-001  '), 'INV-APP-2026-001');
    });

    test('无关内容判无效：网址/协议类/整句文本/过短/过长/空值', () {
      expect(sanitizeScannedCode(null), isNull);
      expect(sanitizeScannedCode(''), isNull);
      expect(sanitizeScannedCode('   '), isNull);
      expect(sanitizeScannedCode('ab'), isNull, reason: '过短不可能是单证编号');
      expect(sanitizeScannedCode('https://example.com/promo?x=1'), isNull);
      expect(sanitizeScannedCode('BEGIN:VCARD'), isNull, reason: '协议类内容');
      expect(sanitizeScannedCode('中欧班列 单证 查询'), isNull, reason: '含空格的整句文本');
      expect(sanitizeScannedCode('a' * 65), isNull, reason: '异常长内容不可信');
    });
  });

  group('权限拒绝降级UI（ScannerErrorView）', () {
    testWidgets('权限拒绝：中文说明用途+引导手动输入，不崩溃', (tester) async {
      var manualTapped = false;
      await tester.pumpWidget(MaterialApp(
        home: Scaffold(
          body: ScannerErrorView(
            error: const MobileScannerException(
                errorCode: MobileScannerErrorCode.permissionDenied),
            onManualInput: () => manualTapped = true,
          ),
        ),
      ));
      expect(find.text(scanPermissionMessage), findsOneWidget);
      expect(find.textContaining('手动输入'), findsWidgets);
      expect(find.text('返回手动输入'), findsOneWidget);
      await tester.tap(find.text('返回手动输入'));
      expect(manualTapped, isTrue, reason: '拒绝权限后有可用的手动输入退路');
    });

    testWidgets('相机其他错误：如实展示错误类型+手动输入退路', (tester) async {
      await tester.pumpWidget(MaterialApp(
        home: Scaffold(
          body: ScannerErrorView(
            error: const MobileScannerException(
                errorCode: MobileScannerErrorCode.genericError),
          ),
        ),
      ));
      expect(find.text('摄像头启动失败，无法扫码'), findsOneWidget);
      expect(find.textContaining('genericError'), findsOneWidget);
      expect(find.text('返回手动输入'), findsOneWidget);
    });
  });

  group('扫码页（BarcodeScannerPage）', () {
    testWidgets('有明确的取消入口和手动输入退路；相机不可用时不白屏不崩溃', (tester) async {
      await tester.pumpWidget(MaterialApp(home: Builder(builder: (context) {
        Future<void> open() => Navigator.push(context,
            MaterialPageRoute<String>(builder: (_) => const BarcodeScannerPage()));
        return Scaffold(
            body: Center(
                child: FilledButton(
                    onPressed: open, child: const Text('打开'))));
      })));
      await tester.tap(find.text('打开'));
      // 用固定pump而非pumpAndSettle：取景占位动画不会停止，页面必须在该状态下可用
      await tester.pump();
      await tester.pump(const Duration(seconds: 1));
      // 扫码页壳子：标题+取消按钮+顶部指引+手动输入退路始终存在
      expect(find.text('扫码速查'), findsOneWidget);
      expect(find.byTooltip('取消扫码'), findsOneWidget);
      expect(find.text('改用手动输入'), findsOneWidget);
      expect(find.textContaining('将单证上的条码/二维码对准取景框'), findsOneWidget);
      // 相机异常时必须给出可读的降级界面（错误视图或占位态），而非白屏
      final nonBlank = find.descendant(
          of: find.byType(Scaffold), matching: find.byWidgetPredicate(
              (w) => w is Text || w is CircularProgressIndicator));
      expect(nonBlank, findsWidgets);
      // 取消返回不崩溃
      await tester.tap(find.byTooltip('取消扫码'));
      await tester.pump();
      expect(find.text('打开'), findsOneWidget);
    });
  });
}
