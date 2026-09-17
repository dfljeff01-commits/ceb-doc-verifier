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

  test('route_countries 编辑后保持字符串列表（F05回归）', () {
    final v = serializeFieldValue('route_countries', '中国、俄罗斯、德国') as List;
    expect(v, ['中国', '俄罗斯', '德国']);
    final v2 = serializeFieldValue('route_countries', '中国,德国');
    expect(v2, ['中国', '德国']);
    expect(serializeFieldValue('route_countries', '  '), isNull);
  });

  test('数值字段编辑后保持num类型，非法输入保留原文', () {
    expect(serializeFieldValue('total_packages', '480'), 480);
    expect(serializeFieldValue('gross_weight_kg', '12300.5'), 12300.5);
    expect(serializeFieldValue('gross_weight_kg', '12300kg'), '12300kg');
    expect(serializeFieldValue('goods_description', ' 陶瓷卫浴洁具 '), '陶瓷卫浴洁具');
    expect(serializeFieldValue('goods_description', ''), isNull);
  });

  test('列表字段显示为顿号连接，空值为空串', () {
    expect(displayFieldValue(['中国', '德国']), '中国、德国');
    expect(displayFieldValue(480), '480');
    expect(displayFieldValue(null), '');
  });

  test('契约字段表：更换单证类型后出现目标类型字段（F05回归）', () {
    expect(docTypeRequiredFields['unknown'], isEmpty);
    expect(docTypeRequiredFields['railway_waybill']!.contains('route_countries'), isTrue);
    expect(docTypeRequiredFields['export_customs_declaration']!.contains('declared_value'), isTrue);
  });
}
