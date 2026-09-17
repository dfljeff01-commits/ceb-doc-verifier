import 'dart:io';

import 'package:ceb_verifier/main.dart';
import 'package:flutter_test/flutter_test.dart';

void main() {
  test('网络错误映射为友好提示', () {
    final msg = friendlyError(const SocketException('refused'));
    expect(msg, contains('无法连接后端'));
  });

  test('DocEntry 转核验引擎文档结构', () {
    final e = DocEntry(
      fileName: 'invoice.jpg',
      docType: 'invoice',
      fields: {'total_packages': 480},
      confidence: {'total_packages': 'high'},
    );
    final doc = e.toDocument();
    expect(doc['doc_type'], 'invoice');
    expect(doc['fields']['total_packages'], 480);
    expect(e.needsReview, isFalse);
  });
}
