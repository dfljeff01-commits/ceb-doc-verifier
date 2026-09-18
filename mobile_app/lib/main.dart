// 中欧班列单证智能核验 · Android 初级版（Flutter）
//
// 功能流（P0）：拍照/相册选图 → 上传后端OCR识别 → 字段预览与人工纠正
//              → 提交核验 → 风险大卡片 + 明细 + AI修正建议
// 后端：复用 FastAPI（POST /ingest/image 与 POST /verify），App只做交互与网络调用。
// 诚实性：识别为后端OCR真实结果，字段可人工纠正；建议以后端返回为准。

import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:flutter/material.dart';
import 'package:http/http.dart' as http;
import 'package:image_picker/image_picker.dart';
import 'package:shared_preferences/shared_preferences.dart';

void main() => runApp(const CebApp());

// ---------------------------------------------------------------- 模型

class DocEntry {
  DocEntry({
    required this.fileName,
    required this.docType,
    required this.fields,
    required this.confidence,
    this.error,
    this.ingestSeconds = 0,
  });

  String fileName;
  String docType;
  Map<String, dynamic> fields;
  Map<String, String> confidence;
  String? error;
  double ingestSeconds;

  bool get needsReview => confidence.values.contains('missing');

  Map<String, dynamic> toDocument() => {
        'doc_type': docType,
        'doc_id': 'APP-$fileName',
        'title': typeTitles[docType] ?? '上传单证 $fileName',
        'fields': fields,
      };
}

const typeTitles = {
  'invoice': '商业发票 Commercial Invoice',
  'packing_list': '装箱单 Packing List',
  'railway_waybill': '国际铁路运单 Railway Consignment Note',
  'export_customs_declaration': '出口报关单 Export Customs Declaration',
  'certificate_of_origin': '原产地证书 Certificate of Origin',
  'unknown': '未识别单证',
};

const fieldTypeNames = {
  'invoice': '商业发票',
  'packing_list': '装箱单',
  'railway_waybill': '铁路运单',
  'export_customs_declaration': '出口报关单',
  'certificate_of_origin': '原产地证书',
  'unknown': '无法识别（请人工选择）',
};

// 各单证类型的契约字段（与后端 doc_contract.REQUIRED_FIELDS 同口径）。
// 编辑页按「已提取字段 ∪ 所选类型的契约字段」渲染——更换单证类型后
// 目标类型的字段立即出现，缺失字段可人工补齐（修复审查 F05）。
const Map<String, List<String>> docTypeRequiredFields = {
  'invoice': [
    'invoice_no', 'consignor_name', 'consignee_name', 'goods_description',
    'total_packages', 'gross_weight_kg', 'total_amount', 'currency',
  ],
  'packing_list': [
    'packing_list_no', 'consignor_name', 'consignee_name', 'goods_description',
    'total_packages', 'gross_weight_kg', 'container_no',
  ],
  'railway_waybill': [
    'waybill_no', 'waybill_type', 'consignor_name', 'consignee_name',
    'route_countries', 'goods_description', 'total_packages',
    'gross_weight_kg', 'container_no',
  ],
  'export_customs_declaration': [
    'declaration_no', 'consignor_name', 'consignee_name', 'goods_description',
    'total_packages', 'gross_weight_kg', 'declared_value', 'currency',
    'waybill_no', 'container_no', 'departure_country', 'destination_country',
  ],
  'certificate_of_origin': [
    'co_no', 'consignor_name', 'consignee_name', 'goods_description',
    'total_packages',
  ],
  'unknown': [],
};

const numericFieldNames = {
  'total_packages', 'gross_weight_kg', 'net_weight_kg', 'total_amount',
  'declared_value',
};

const listFieldNames = {'route_countries'};

/// 编辑框文本 → 字段真实类型（保存时调用）。
/// route_countries 保持字符串列表（修复"列表被编辑成字符串"缺陷）；
/// 数值字段解析为 num；其余为字符串；空值返回 null（删除该字段）。
dynamic serializeFieldValue(String key, String raw) {
  final text = raw.trim();
  if (text.isEmpty) return null;
  if (listFieldNames.contains(key)) {
    final parts = text
        .split(RegExp(r'[、，,]|->|→'))
        .map((p) => p.trim())
        .where((p) => p.isNotEmpty)
        .toList();
    return parts.isEmpty ? null : parts;
  }
  if (numericFieldNames.contains(key)) {
    return num.tryParse(text) ?? text; // 解析失败保留原文，由后端口径判待复核
  }
  return text;
}

/// 字段值 → 编辑框显示文本（列表用顿号连接，其余 toString）。
String displayFieldValue(dynamic v) {
  if (v == null) return '';
  if (v is List) return v.join('、');
  return '$v';
}

class VerifyReport {
  VerifyReport.fromJson(Map<String, dynamic> j)
      : summary = j['summary'],
        risk = j['risk'],
        results = List<Map<String, dynamic>>.from(j['results']),
        suggestions = List<String>.from(j['suggestions'] ?? []);

  final Map<String, dynamic> summary;
  final Map<String, dynamic> risk;
  final List<Map<String, dynamic>> results;
  final List<String> suggestions;
}

// ---------------------------------------------------------------- API 客户端

class ApiClient {
  ApiClient(this.baseUrl);

  final String baseUrl;

  Future<Map<String, dynamic>> ingestImage(List<int> bytes, String fileName) async {
    final uri = Uri.parse('$baseUrl/ingest/image');
    final req = http.MultipartRequest('POST', uri)
      ..files.add(http.MultipartFile.fromBytes('file', bytes, filename: fileName));
    final resp = await req.send().timeout(const Duration(seconds: 60));
    final body = await resp.stream.bytesToString();
    if (resp.statusCode == 503) {
      throw ApiException('后端未安装OCR环境（$fileName）：${_detail(body)}');
    }
    if (resp.statusCode != 200) {
      throw ApiException('识别失败（HTTP ${resp.statusCode}）：${_detail(body)}');
    }
    return jsonDecode(body) as Map<String, dynamic>;
  }

  Future<VerifyReport> verify(List<DocEntry> docs) async {
    final uri = Uri.parse('$baseUrl/verify');
    final resp = await http
        .post(uri,
            headers: {'Content-Type': 'application/json'},
            body: jsonEncode({
              'batch_id': 'app_upload',
              'batch_name': 'App拍照识别批次',
              'documents': docs.map((d) => d.toDocument()).toList(),
            }))
        .timeout(const Duration(seconds: 30));
    if (resp.statusCode != 200) {
      throw ApiException('核验失败（HTTP ${resp.statusCode}）：${_detail(resp.body)}');
    }
    return VerifyReport.fromJson(jsonDecode(resp.body));
  }

  static String _detail(String body) {
    try {
      final j = jsonDecode(body);
      return (j['detail'] ?? body).toString();
    } catch (_) {
      return body.length > 120 ? body.substring(0, 120) : body;
    }
  }
}

class ApiException implements Exception {
  ApiException(this.message);
  final String message;
}

String friendlyError(Object e) {
  if (e is SocketException || e is HttpException) {
    return '无法连接后端服务：请检查设置中的API地址是否正确、'
        '后端是否已启动（uvicorn api:app --host 0.0.0.0 --port 8000），'
        '以及手机与后端是否在同一局域网。';
  }
  if (e is TimeoutException) {
    return '请求超时：后端响应过慢（OCR可能正在处理），请稍后重试。';
  }
  if (e is FormatException) {
    return 'API地址格式不正确，请检查设置（应形如 http://192.168.x.x:8000）。';
  }
  return e.toString();
}

// ---------------------------------------------------------------- App

class CebApp extends StatelessWidget {
  const CebApp({super.key});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: '中欧班列单证核验',
      debugShowCheckedModeBanner: false,
      theme: ThemeData(
        colorScheme: ColorScheme.fromSeed(seedColor: const Color(0xFF0B5394)),
        useMaterial3: true,
      ),
      home: const HomePage(),
    );
  }
}

// ---------------------------------------------------------------- 首页

class HomePage extends StatefulWidget {
  const HomePage({super.key});

  @override
  State<HomePage> createState() => _HomePageState();
}

class _HomePageState extends State<HomePage> {
  String _baseUrl = 'http://192.168.5.44:8000';
  final List<DocEntry> _docs = [];
  bool _loading = false;
  String? _loadingMsg;

  @override
  void initState() {
    super.initState();
    _loadSettings();
  }

  Future<void> _loadSettings() async {
    final prefs = await SharedPreferences.getInstance();
    if (mounted) {
      setState(() => _baseUrl = prefs.getString('api_base') ?? _baseUrl);
    }
  }

  Future<void> _saveBaseUrl(String url) async {
    setState(() => _baseUrl = url);
    final prefs = await SharedPreferences.getInstance();
    await prefs.setString('api_base', url);
  }

  Future<void> _pick(ImageSource source) async {
    final picker = ImagePicker();
    List<XFile> files;
    try {
      if (source == ImageSource.gallery) {
        files = await picker.pickMultiImage(imageQuality: 88);
      } else {
        final f = await picker.pickImage(source: source, imageQuality: 88);
        files = f == null ? [] : [f];
      }
    } catch (e) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(content: Text('打开相机/相册失败：$e'), backgroundColor: Colors.red));
      return;
    }
    if (files.isEmpty) return;
    await _ingest(files);
  }

  Future<void> _ingest(List<XFile> files) async {
    setState(() {
      _loading = true;
      _loadingMsg = '正在上传并识别（${files.length}张）…';
    });
    final client = ApiClient(_baseUrl);
    final okDocs = <DocEntry>[];
    try {
      for (final f in files) {
        setState(() => _loadingMsg = '识别中：${f.name}');
        final bytes = await f.readAsBytes();
        final j = await client.ingestImage(bytes, f.name);
        final entry = DocEntry(
          fileName: f.name,
          docType: j['doc_type'] ?? 'unknown',
          fields: Map<String, dynamic>.from(j['fields'] ?? {}),
          confidence: Map<String, String>.from(j['field_confidence'] ?? {}),
          ingestSeconds: (j['elapsed_seconds'] ?? 0).toDouble(),
        );
        if ((j['error'] as String?) != null) {
          entry.error = j['error'] as String;
        }
        okDocs.add(entry);
      }
    } catch (e) {
      if (!mounted) return;
      showDialog(
          context: context,
          builder: (_) => AlertDialog(
                title: const Text('识别失败'),
                content: Text(friendlyError(e)),
                actions: [
                  TextButton(
                      onPressed: () => Navigator.pop(context), child: const Text('知道了'))
                ],
              ));
    } finally {
      if (mounted) {
        setState(() {
          _loading = false;
          _docs.addAll(okDocs);
        });
      }
    }
  }

  Future<void> _verify() async {
    setState(() => _loading = true);
    try {
      final report = await ApiClient(_baseUrl).verify(_docs);
      if (!mounted) return;
      await Navigator.push(
          context, MaterialPageRoute(builder: (_) => ResultPage(report: report)));
    } catch (e) {
      if (!mounted) return;
      showDialog(
          context: context,
          builder: (_) => AlertDialog(
                title: const Text('核验失败'),
                content: Text(friendlyError(e)),
                actions: [
                  TextButton(
                      onPressed: () => Navigator.pop(context), child: const Text('知道了'))
                ],
              ));
    } finally {
      if (mounted) setState(() => _loading = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: const Text('🚂 中欧班列单证核验'),
        actions: [
          IconButton(
              icon: const Icon(Icons.settings),
              tooltip: '后端地址设置',
              onPressed: () async {
                final url = await Navigator.push<String>(context,
                    MaterialPageRoute(builder: (_) => SettingsPage(initial: _baseUrl)));
                if (url != null && url.isNotEmpty) await _saveBaseUrl(url);
              }),
        ],
      ),
      body: Stack(children: [
        ListView(padding: const EdgeInsets.all(16), children: [
          Card(
            color: const Color(0xFFE3F2FD),
            child: const Padding(
              padding: EdgeInsets.all(12),
              child: Text(
                '中欧班列欧洲枢纽换装平均耗时38.7小时，单证不一致是主因之一——'
                '拍下您的发票/箱单/运单/报关单，发运前几秒完成交叉核验。',
                style: TextStyle(fontSize: 13.5),
              ),
            ),
          ),
          const SizedBox(height: 12),
          FilledButton.icon(
            icon: const Icon(Icons.photo_camera, size: 22),
            label: const Text('拍照识别单证', style: TextStyle(fontSize: 17)),
            style: FilledButton.styleFrom(
                padding: const EdgeInsets.symmetric(vertical: 14)),
            onPressed: _loading ? null : () => _pick(ImageSource.camera),
          ),
          const SizedBox(height: 10),
          OutlinedButton.icon(
            icon: const Icon(Icons.photo_library, size: 22),
            label: const Text('从相册选择（可多选）', style: TextStyle(fontSize: 17)),
            style: OutlinedButton.styleFrom(
                padding: const EdgeInsets.symmetric(vertical: 14)),
            onPressed: _loading ? null : () => _pick(ImageSource.gallery),
          ),
          const SizedBox(height: 6),
          Text('后端：$_baseUrl',
              style: const TextStyle(fontSize: 11.5, color: Colors.grey)),
          const SizedBox(height: 10),
          if (_docs.isNotEmpty) ...[
            Text('已识别单证（${_docs.length}份）——点击卡片可编辑字段',
                style:
                    const TextStyle(fontWeight: FontWeight.w600, fontSize: 14)),
            const SizedBox(height: 6),
            for (var i = 0; i < _docs.length; i++)
              _DocCard(
                entry: _docs[i],
                onTap: () async {
                  final updated = await Navigator.push<DocEntry>(
                      context,
                      MaterialPageRoute(
                          builder: (_) => EditFieldsPage(entry: _clone(_docs[i]))));
                  if (updated != null) {
                    setState(() => _docs[i] = updated);
                  }
                },
                onRemove: () => setState(() => _docs.removeAt(i)),
              ),
            const SizedBox(height: 14),
            FilledButton.icon(
              icon: const Icon(Icons.verified),
              label: const Text('开始核验', style: TextStyle(fontSize: 17)),
              style: FilledButton.styleFrom(
                  padding: const EdgeInsets.symmetric(vertical: 14),
                  backgroundColor: Colors.red.shade700),
              onPressed: _loading ? null : _verify,
            ),
            const SizedBox(height: 8),
            TextButton(
                onPressed: () => setState(() => _docs.clear()),
                child: const Text('清空全部单证')),
          ],
        ]),
        if (_loading)
          Container(
            color: Colors.black38,
            child: Center(
              child: Card(
                child: Padding(
                  padding: const EdgeInsets.all(20),
                  child: Column(
                    mainAxisSize: MainAxisSize.min,
                    children: [
                      const CircularProgressIndicator(),
                      const SizedBox(height: 12),
                      Text(_loadingMsg ?? '处理中…'),
                    ],
                  ),
                ),
              ),
            ),
          ),
      ]),
    );
  }

  static DocEntry _clone(DocEntry e) => DocEntry(
        fileName: e.fileName,
        docType: e.docType,
        fields: Map<String, dynamic>.from(e.fields),
        confidence: Map<String, String>.from(e.confidence),
        ingestSeconds: e.ingestSeconds,
      );
}

// ---------------------------------------------------------------- 单证卡片

class _DocCard extends StatelessWidget {
  const _DocCard({required this.entry, required this.onTap, required this.onRemove});

  final DocEntry entry;
  final VoidCallback onTap;
  final VoidCallback onRemove;

  @override
  Widget build(BuildContext context) {
    final missing =
        entry.confidence.entries.where((e) => e.value == 'missing').length;
    final color = entry.error != null
        ? Colors.red
        : (entry.needsReview ? Colors.orange : Colors.green);
    return Card(
      margin: const EdgeInsets.symmetric(vertical: 4),
      child: ListTile(
        leading: CircleAvatar(
            backgroundColor: color.withValues(alpha: 0.15),
            child: Icon(Icons.description, color: color)),
        title: Text(
            '${fieldTypeNames[entry.docType] ?? entry.docType} · ${entry.fileName}',
            style: const TextStyle(fontSize: 14, fontWeight: FontWeight.w600)),
        subtitle: Text(
          entry.error != null
              ? '识别失败：${entry.error}'
              : (missing > 0
                  ? '⚠️ $missing 个字段提取失败/需人工核对（${entry.fields.length} 个已提取）'
                  : '${entry.fields.length} 个字段高置信度提取 · ${entry.ingestSeconds.toStringAsFixed(1)}s'),
          style: TextStyle(fontSize: 12, color: color),
        ),
        trailing:
            IconButton(icon: const Icon(Icons.delete_outline), onPressed: onRemove),
        onTap: onTap,
      ),
    );
  }
}

// ---------------------------------------------------------------- 字段编辑页

class EditFieldsPage extends StatefulWidget {
  const EditFieldsPage({super.key, required this.entry});

  final DocEntry entry;

  @override
  State<EditFieldsPage> createState() => _EditFieldsPageState();
}

class _EditFieldsPageState extends State<EditFieldsPage> {
  late DocEntry _entry;
  late String _docType;
  final Map<String, TextEditingController> _controllers = {};

  @override
  void initState() {
    super.initState();
    _entry = widget.entry;
    _docType = _entry.docType;
  }

  /// 当前应渲染的字段 = 已提取字段 ∪ 置信度字段 ∪ 所选类型契约字段。
  Set<String> _visibleKeys() {
    final keys = <String>{..._entry.fields.keys, ..._entry.confidence.keys};
    keys.addAll(docTypeRequiredFields[_docType] ?? const []);
    return keys;
  }

  @override
  void dispose() {
    for (final c in _controllers.values) {
      c.dispose();
    }
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final keys = _visibleKeys().toList()..sort();
    return Scaffold(
      appBar: AppBar(title: Text('编辑字段 · ${_entry.fileName}')),
      body: ListView(padding: const EdgeInsets.all(14), children: [
        DropdownButtonFormField<String>(
          initialValue: _docType,
          decoration: const InputDecoration(
              labelText: '单证类型（自动判断，可纠正；更换后出现对应字段）',
              border: OutlineInputBorder()),
          items: fieldTypeNames.entries
              .map((e) => DropdownMenuItem(value: e.key, child: Text(e.value)))
              .toList(),
          onChanged: (v) => setState(() => _docType = v ?? _docType),
        ),
        const SizedBox(height: 6),
        Text('低置信度/提取失败的字段以 ⚠️ 标注，缺失字段可直接填写；'
                '经停国家用顿号或逗号分隔（保存为列表）。',
            style: TextStyle(fontSize: 12, color: Colors.orange.shade800)),
        const SizedBox(height: 10),
        for (final k in keys)
          Padding(
            padding: const EdgeInsets.symmetric(vertical: 5),
            child: TextField(
              controller: _controllers.putIfAbsent(k,
                  () => TextEditingController(text: displayFieldValue(_entry.fields[k]))),
              keyboardType: numericFieldNames.contains(k)
                  ? const TextInputType.numberWithOptions(decimal: true)
                  : TextInputType.text,
              decoration: InputDecoration(
                labelText: k,
                border: const OutlineInputBorder(),
                suffixIcon: (_entry.confidence[k] == 'missing' ||
                        _entry.confidence[k] == 'review')
                    ? const Tooltip(
                        message: '提取失败/口径存疑，需人工核对',
                        child: Icon(Icons.warning_amber_rounded,
                            color: Colors.orange))
                    : _entry.confidence[k] == 'high'
                        ? const Tooltip(
                            message: '高置信度自动提取',
                            child: Icon(Icons.check_circle_outline,
                                color: Colors.green))
                        : const Tooltip(
                            message: '契约字段，请人工填写',
                            child: Icon(Icons.edit_note, color: Colors.blue)),
              ),
            ),
          ),
        const SizedBox(height: 12),
        FilledButton.icon(
          icon: const Icon(Icons.save),
          label: const Text('保存并返回'),
          onPressed: () {
            _entry.docType = _docType;
            final saved = <String, dynamic>{..._entry.fields};
            for (final k in keys) {
              final value = serializeFieldValue(k, _controllers[k]!.text);
              if (value == null) {
                saved.remove(k);
              } else {
                saved[k] = value;
              }
            }
            _entry.fields = saved;
            _entry.confidence.updateAll((k, v) =>
                (_entry.fields[k] != null && '${_entry.fields[k]}'.isNotEmpty)
                    ? 'high'
                    : v);
            Navigator.pop(context, _entry);
          },
        ),
      ]),
    );
  }
}

// ---------------------------------------------------------------- 结果页

class ResultPage extends StatelessWidget {
  const ResultPage({super.key, required this.report});

  final VerifyReport report;

  static const gradeColors = {
    'low': Color(0xFF1B5E20),
    'medium': Color(0xFF8D6E00),
    'high': Color(0xFFB71C1C),
  };
  static const statusColors = {
    'PASS': Color(0xFF1B5E20),
    'WARNING': Color(0xFF8D6E00),
    'FAIL': Color(0xFFB71C1C),
  };

  @override
  Widget build(BuildContext context) {
    final risk = report.risk;
    final score = (risk['score'] as num).toInt();
    final grade = risk['grade'] as String;
    final color = gradeColors[grade] ?? Colors.grey;
    final breakdown = List<Map<String, dynamic>>.from(risk['breakdown'] ?? []);

    return Scaffold(
      appBar: AppBar(title: const Text('核验结果')),
      body: ListView(padding: const EdgeInsets.all(14), children: [
        Card(
          elevation: 4,
          child: Padding(
            padding: const EdgeInsets.symmetric(vertical: 20),
            child: Column(children: [
              SizedBox(
                width: 190,
                height: 190,
                child: Stack(alignment: Alignment.center, children: [
                  SizedBox(
                    width: 190,
                    height: 190,
                    child: CircularProgressIndicator(
                      value: score / 100,
                      strokeWidth: 16,
                      strokeCap: StrokeCap.round,
                      backgroundColor: Colors.grey.shade200,
                      valueColor: AlwaysStoppedAnimation(color),
                    ),
                  ),
                  Column(mainAxisSize: MainAxisSize.min, children: [
                    Text('$score',
                        style: TextStyle(
                            fontSize: 56,
                            fontWeight: FontWeight.bold,
                            color: color,
                            height: 1.0)),
                    const Text('单证组风险分',
                        style: TextStyle(fontSize: 12, color: Colors.grey)),
                    const SizedBox(height: 2),
                    Container(
                      padding: const EdgeInsets.symmetric(
                          horizontal: 14, vertical: 3),
                      decoration: BoxDecoration(
                          color: color.withValues(alpha: 0.12),
                          borderRadius: BorderRadius.circular(12)),
                      child: Text(risk['grade_label'] as String,
                          style: TextStyle(
                              color: color,
                              fontWeight: FontWeight.bold,
                              fontSize: 15)),
                    ),
                  ]),
                ]),
              ),
              const SizedBox(height: 8),
              Text(
                '共 ${report.summary['total']} 项检查 · ✅${report.summary['pass']} '
                '⚠️${report.summary['warning']} ⛔${report.summary['fail']}',
                style: const TextStyle(fontSize: 14),
              ),
            ]),
          ),
        ),
        if (breakdown.isNotEmpty) ...[
          const Padding(
            padding: EdgeInsets.only(top: 8, bottom: 4),
            child: Text('分数构成（可解释分解）',
                style: TextStyle(fontWeight: FontWeight.bold, fontSize: 14)),
          ),
          Wrap(
            spacing: 6,
            runSpacing: 6,
            children: breakdown
                .map((b) => Chip(
                      label: Text('${b['reason']} +${b['points']}',
                          style: const TextStyle(fontSize: 12)),
                      backgroundColor: (b['status'] == 'FAIL'
                              ? Colors.red
                              : Colors.orange)
                          .withValues(alpha: 0.1),
                    ))
                .toList(),
          ),
        ],
        const Padding(
          padding: EdgeInsets.only(top: 14, bottom: 4),
          child:
              Text('核验明细', style: TextStyle(fontWeight: FontWeight.bold, fontSize: 14)),
        ),
        for (final r in report.results)
          Card(
            margin: const EdgeInsets.symmetric(vertical: 4),
            child: Container(
              decoration: BoxDecoration(
                border: Border(
                    left: BorderSide(
                        color: statusColors[r['status']] ?? Colors.grey,
                        width: 5)),
                borderRadius: BorderRadius.circular(8),
              ),
              padding: const EdgeInsets.all(12),
              child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Row(children: [
                      Text('${r['status']}  ',
                          style: TextStyle(
                              color: statusColors[r['status']] ?? Colors.grey,
                              fontWeight: FontWeight.bold)),
                      Expanded(
                          child: Text('${r['check_name']}',
                              style: const TextStyle(
                                  fontWeight: FontWeight.w600, fontSize: 13.5))),
                    ]),
                    const SizedBox(height: 4),
                    Text('${r['detail']}',
                        style: const TextStyle(
                            fontSize: 12.5, color: Colors.black87)),
                  ]),
            ),
          ),
        if (report.suggestions.isNotEmpty) ...[
          const Padding(
            padding: EdgeInsets.only(top: 14, bottom: 4),
            child: Text('AI 修正建议',
                style: TextStyle(fontWeight: FontWeight.bold, fontSize: 14)),
          ),
          for (final s in report.suggestions)
            Card(
              color: const Color(0xFFFFF8E1),
              margin: const EdgeInsets.symmetric(vertical: 4),
              child: Padding(
                padding: const EdgeInsets.all(12),
                child: Text(s, style: const TextStyle(fontSize: 12.5)),
              ),
            ),
        ],
        const SizedBox(height: 10),
        const Text(
          '本工具为初级版（内部试用）：识别为OCR结果（可人工纠正），规则为简化口径，结论供人工复核参考，不构成自动放行或法律依据。',
          textAlign: TextAlign.center,
          style: TextStyle(fontSize: 11, color: Colors.grey),
        ),
      ]),
    );
  }
}

// ---------------------------------------------------------------- 设置页

class SettingsPage extends StatefulWidget {
  const SettingsPage({super.key, required this.initial});

  final String initial;

  @override
  State<SettingsPage> createState() => _SettingsPageState();
}

class _SettingsPageState extends State<SettingsPage> {
  late final TextEditingController _controller;

  @override
  void initState() {
    super.initState();
    _controller = TextEditingController(text: widget.initial);
  }

  @override
  void dispose() {
    _controller.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: const Text('后端地址设置')),
      body: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
          TextField(
            controller: _controller,
            keyboardType: TextInputType.url,
            decoration: const InputDecoration(
              labelText: 'API Base URL',
              hintText: 'http://192.168.x.x:8000',
              border: OutlineInputBorder(),
              helperText: '填写运行核验后端的电脑局域网IP与端口（以现场网络为准）',
            ),
          ),
          const SizedBox(height: 12),
          FilledButton(
            child: const Text('保存'),
            onPressed: () {
              final url = _controller.text.trim().replaceAll(RegExp(r'/+$'), '');
              if (!url.startsWith('http')) {
                ScaffoldMessenger.of(context).showSnackBar(const SnackBar(
                    content: Text('地址需以 http:// 开头'),
                    backgroundColor: Colors.red));
                return;
              }
              Navigator.pop(context, url);
            },
          ),
          const SizedBox(height: 20),
          const Card(
            color: Color(0xFFF5F5F5),
            child: Padding(
              padding: EdgeInsets.all(12),
              child: Text(
                '提示：后端启动命令示例\n'
                '  uvicorn api:app --host 0.0.0.0 --port 8000\n'
                '手机需与后端电脑连接同一Wi-Fi/局域网。\n'
                'Android模拟器访问本机请用 http://10.0.2.2:8000',
                style: TextStyle(fontSize: 12.5, height: 1.6),
              ),
            ),
          ),
        ]),
      ),
    );
  }
}
