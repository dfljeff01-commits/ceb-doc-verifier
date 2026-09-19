// 网络错误映射与弱网重试判定测试。
// （旧版针对字段编辑契约的用例已随"移除字段编辑界面"一并删除，
//   编辑序列化能力保留在后端 /ingest/image + 电脑端网页中。）
import 'dart:async';
import 'dart:io';

import 'package:ceb_verifier/main.dart';
import 'package:flutter_test/flutter_test.dart';

void main() {
  group('网络错误 → 现场可读提示', () {
    test('连接失败提示已暂存+自动重传+检查设置', () {
      final msg = friendlyError(SocketException('refused'));
      expect(msg, contains('已暂存'));
      expect(msg, contains('自动重传'));
      expect(msg, contains('API地址'));
    });

    test('超时提示弱网暂存语义', () {
      final msg = friendlyError(TimeoutException('t'));
      expect(msg, contains('网络缓慢'));
      expect(msg, contains('已暂存'));
    });

    test('后端业务错误（ApiException）原样透传', () {
      expect(friendlyError(ApiException('照片过大')), '照片过大');
    });

    test('未知错误兜底', () {
      expect(friendlyError(StateError('x')), contains('x'));
    });
  });

  group('弱网重试判定（isRetryable）', () {
    test('Socket/Timeout/Http错误可重试', () {
      expect(isRetryable(SocketException('down')), isTrue);
      expect(isRetryable(TimeoutException('t')), isTrue);
      expect(isRetryable(HttpException('bad')), isTrue);
    });

    test('413/422类契约错误不可重试（重试也不会成功）', () {
      expect(isRetryable(ApiException('HTTP 413: 照片过大')), isFalse);
    });
  });
}
