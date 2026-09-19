// 轻量摘要模型测试：App结果展示的唯一形态是一句话反馈（红/黄/绿+一句关键问题+批次编号），
// 与后端 /mobile/quick-check、/mobile/lookup 的响应契约绑定。
import 'dart:io';

import 'package:ceb_verifier/main.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

QuickCheckResult fullRed() => QuickCheckResult.fromJson({
      'batch_id': 'MB-20260920-143001-8A3C',
      'created_at': '2026-09-20T14:30:01',
      'risk_grade': 'high',
      'risk_level': 'red',
      'risk_label': '高风险',
      'risk_score': 88,
      'one_line': '必需单证齐全性检查：缺少必需单证：装箱单、铁路运单',
      'doc_count': 2,
      'doc_types': ['invoice', 'unknown'],
      'identity_numbers': {'invoice_no': ['INV-100']},
      'detail_hint': '请在电脑端核验网页输入批次编号 MB-20260920-143001-8A3C 查看完整报告',
    });

void main() {
  group('QuickCheckResult', () {
    test('解析后端轻量摘要JSON', () {
      final r = fullRed();
      expect(r.batchId, 'MB-20260920-143001-8A3C');
      expect(r.riskLevel, 'red');
      expect(r.riskLabel, '高风险');
      expect(r.riskScore, 88);
      expect(r.docCount, 2);
      expect(r.oneLine, contains('缺少必需单证'));
      expect(r.detailHint, contains('电脑端'));
    });

    test('红黄绿映射到横幅颜色与图标', () {
      expect(fullRed().bannerColor, const Color(0xFFB71C1C));
      final yellow = QuickCheckResult.fromJson(
          {...fullRed().toJson(), 'risk_level': 'yellow', 'risk_label': '中风险'});
      expect(yellow.bannerColor, const Color(0xFF8D6E00));
      final green = QuickCheckResult.fromJson(
          {...fullRed().toJson(), 'risk_level': 'green', 'risk_label': '低风险'});
      expect(green.bannerColor, const Color(0xFF1B5E20));
    });

    test('JSON往返（本地最近反馈缓存用）', () {
      final r = fullRed();
      final back = QuickCheckResult.fromJson(r.toJson());
      expect(back.batchId, r.batchId);
      expect(back.riskLevel, r.riskLevel);
      expect(back.oneLine, r.oneLine);
    });

    test('字段缺失不崩溃（后端降级容错）', () {
      final r = QuickCheckResult.fromJson({});
      expect(r.batchId, '');
      expect(r.riskLevel, 'green');
      expect(r.riskScore, 0);
    });
  });

  group('LookupMatch', () {
    test('解析现场速查命中项', () {
      final m = LookupMatch.fromJson({
        'batch_id': 'MB-20260919-090000-11AA',
        'created_at': '2026-09-19T09:00:00',
        'risk_level': 'yellow',
        'risk_label': '中风险',
        'one_line': '毛重一致性（容差1%）：毛重超出1%容差',
        'doc_count': 5,
      });
      expect(m.batchId, 'MB-20260919-090000-11AA');
      expect(m.riskLevel, 'yellow');
      expect(m.docCount, 5);
      expect(m.dotColor, const Color(0xFF8D6E00));
    });
  });

  group('网络错误处理', () {
    test('可重试错误（弱网）判定', () {
      expect(isRetryable(SocketException('net down')), isTrue);
      expect(isRetryable(ApiException('HTTP 413: 照片过大')), isFalse);
    });
  });
}
