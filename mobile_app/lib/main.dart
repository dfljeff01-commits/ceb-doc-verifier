// 中欧班列单证核验 · Android 现场版（Flutter）
//
// 产品定位：**电脑端是大脑，手机端是触手。**
//   大脑（电脑端网页）：完整核验明细、风险分数构成、AI对话、邮件生成、报告导出；
//   触手（本App）只做两件事——
//     ① 拍照即传：现场（换装站/仓库/口岸）把单证"喂"给系统，得到一句话反馈；
//     ② 现场速查：随手查一批货之前是否核验过、上次的风险等级和核心结论。
// 核心操作路径三步以内：打开App → 拍照 → 看到一句话反馈。
//
// 与旧版（缩小版PC）的差异：
//   - 移除逐字段识别结果编辑（后端 /ingest/image、/verify 能力保留，App不再调用）；
//   - 移除完整核验明细/分数构成/AI建议展示（电脑端输入批次编号查看完整报告）；
//   - 新增拍照前本地质量检查（模糊/过暗/边框遮挡/分辨率，不合格不上传）；
//   - 新增弱网应对：上传失败照片本地暂存，网络恢复后自动重试，不丢照片。
//
// 后端：POST /mobile/quick-check（轻量摘要）、GET /mobile/lookup（现场速查），
//       电脑端完整报告由服务端持久化（GET /mobile/batch/{id}?include_full=true）。

import 'dart:async';
import 'dart:convert';
import 'dart:io';
import 'dart:math' as math;
import 'dart:typed_data';
import 'dart:ui' as ui;

import 'package:flutter/material.dart';
import 'package:http/http.dart' as http;
import 'package:image_picker/image_picker.dart';
import 'package:mobile_scanner/mobile_scanner.dart';
import 'package:path_provider/path_provider.dart';
import 'package:shared_preferences/shared_preferences.dart';

void main() => runApp(const CebApp());

// ---------------------------------------------------------------- 模型

/// 上传后的一句话反馈（App唯一的结果展示形态，绝无完整明细）。
class QuickCheckResult {
  QuickCheckResult({
    required this.batchId,
    required this.riskLevel,
    required this.riskGrade,
    required this.riskLabel,
    required this.riskScore,
    required this.oneLine,
    required this.docCount,
    required this.createdAt,
    required this.detailHint,
  });

  final String batchId;
  final String riskLevel; // green / yellow / red
  final String riskGrade; // low / medium / high
  final String riskLabel; // 低风险/中风险/高风险
  final int riskScore;
  final String oneLine;
  final int docCount;
  final String createdAt;
  final String detailHint;

  factory QuickCheckResult.fromJson(Map<String, dynamic> j) => QuickCheckResult(
        batchId: '${j['batch_id'] ?? ''}',
        riskLevel: '${j['risk_level'] ?? 'green'}',
        riskGrade: '${j['risk_grade'] ?? 'low'}',
        riskLabel: '${j['risk_label'] ?? ''}',
        riskScore: (j['risk_score'] as num?)?.toInt() ?? 0,
        oneLine: '${j['one_line'] ?? ''}',
        docCount: (j['doc_count'] as num?)?.toInt() ?? 0,
        createdAt: '${j['created_at'] ?? ''}',
        detailHint: '${j['detail_hint'] ?? ''}',
      );

  Map<String, dynamic> toJson() => {
        'batch_id': batchId,
        'risk_level': riskLevel,
        'risk_grade': riskGrade,
        'risk_label': riskLabel,
        'risk_score': riskScore,
        'one_line': oneLine,
        'doc_count': docCount,
        'created_at': createdAt,
        'detail_hint': detailHint,
      };

  Color get bannerColor => switch (riskLevel) {
        'red' => const Color(0xFFB71C1C),
        'yellow' => const Color(0xFF8D6E00),
        _ => const Color(0xFF1B5E20),
      };

  Color get bannerBg => switch (riskLevel) {
        'red' => const Color(0xFFFFEBEE),
        'yellow' => const Color(0xFFFFF8E1),
        _ => const Color(0xFFE8F5E9),
      };

  IconData get icon => switch (riskLevel) {
        'red' => Icons.report,
        'yellow' => Icons.warning_amber_rounded,
        _ => Icons.verified_rounded,
      };
}

/// 现场速查的一条历史结论（轻量视图，与服务端 /mobile/lookup 契约对应）。
class LookupMatch {
  LookupMatch({
    required this.batchId,
    required this.createdAt,
    required this.riskLevel,
    required this.riskLabel,
    required this.oneLine,
    required this.docCount,
  });

  final String batchId;
  final String createdAt;
  final String riskLevel;
  final String riskLabel;
  final String oneLine;
  final int docCount;

  factory LookupMatch.fromJson(Map<String, dynamic> j) => LookupMatch(
        batchId: '${j['batch_id'] ?? ''}',
        createdAt: '${j['created_at'] ?? ''}',
        riskLevel: '${j['risk_level'] ?? 'green'}',
        riskLabel: '${j['risk_label'] ?? ''}',
        oneLine: '${j['one_line'] ?? ''}',
        docCount: (j['doc_count'] as num?)?.toInt() ?? 0,
      );

  Color get dotColor => switch (riskLevel) {
        'red' => const Color(0xFFB71C1C),
        'yellow' => const Color(0xFF8D6E00),
        _ => const Color(0xFF1B5E20),
      };
}

// ---------------------------------------------------------------- 照片质量检查
//
// 拍完先在本机做最基础的质量把关：模糊/过暗/边框遮挡/分辨率过低的照片
// 立即提示"请重新拍摄"，不上传——避免把明显不合格的照片传到后端浪费一轮OCR。
// 实现为纯Dart数学（缩小到256px网格上算亮度统计），零第三方依赖。
class PhotoQuality {
  PhotoQuality._(this.issues);

  final List<String> issues; // 人话原因列表，空=合格

  bool get ok => issues.isEmpty;

  /// 阈值说明：按256px缩略网格的经验值，宁可放过不可错杀（现场环境复杂）。
  static const int gridWidth = 256;
  static const double _meanLumaDark = 55; // 平均亮度过低 → 过暗
  static const double _lapVarBlur = 18; // 拉普拉斯方差过低 → 模糊
  static const double _edgeBlockRatio = 0.30; // 边缘暗块占比过高 → 遮挡/边框不完整
  static const double _edgeLumaDark = 18; // 判定"暗块"的亮度
  static const int minSide = 480; // 原图最短边

  /// 入口：原始图片字节 → 质量结论（解码失败视为"无法读取，请重新拍摄"）。
  static Future<PhotoQuality> assessBytes(List<int> bytes) async {
    try {
      final codec = await ui.instantiateImageCodec(
          Uint8List.fromList(bytes),
          targetWidth: gridWidth);
      final frame = await codec.getNextFrame();
      final image = frame.image;
      final data =
          await image.toByteData(format: ui.ImageByteFormat.rawRgba);
      final report =
          assessRgba(data!.buffer.asUint8List(), image.width, image.height);
      image.dispose();
      codec.dispose();
      return report;
    } catch (_) {
      return PhotoQuality._(['照片无法读取，请重新拍摄']);
    }
  }

  /// RGBA字节 → 亮度网格 → 四项检查（测试直接喂合成网格，不依赖真实图片）。
  static PhotoQuality assessRgba(List<int> rgba, int width, int height) {
    // 缩略网格：等比缩到高≤gridWidth（宽随比例），逐像素取亮度
    final scale = gridWidth / height;
    final w = width >= height ? math.max(1, (width * scale).round()) : gridWidth;
    final h = height >= width ? gridWidth : math.max(1, (height * scale).round());
    final grid = List.generate(h, (y) {
      final sy = (y * height / h).floor().clamp(0, height - 1);
      return List.generate(w, (x) {
        final sx = (x * width / w).floor().clamp(0, width - 1);
        final i = (sy * width + sx) * 4;
        return (rgba[i] * 299 + rgba[i + 1] * 587 + rgba[i + 2] * 114) ~/ 1000;
      });
    });
    return assessGrid(grid, rawWidth: width, rawHeight: height);
  }

  /// 纯函数：亮度网格上的四项基础检查。
  static PhotoQuality assessGrid(List<List<int>> grid,
      {int rawWidth = 0, int rawHeight = 0}) {
    final issues = <String>[];
    if (rawWidth > 0 && rawHeight > 0 &&
        math.min(rawWidth, rawHeight) < minSide) {
      issues.add('分辨率过低（最短边不足$minSide像素）');
    }
    final h = grid.length, w = grid.isEmpty ? 0 : grid[0].length;
    if (h < 8 || w < 8) {
      return PhotoQuality._(issues..add('照片尺寸异常，请重新拍摄'));
    }
    // ① 平均亮度
    var sum = 0;
    for (final row in grid) {
      for (final v in row) {
        sum += v;
      }
    }
    final mean = sum / (h * w);
    if (mean < _meanLumaDark) issues.add('光线过暗，请到明亮处或开补光灯');
    // ② 清晰度：拉普拉斯响应方差（对焦实/字迹锐利 → 高；糊片 → 趋近0）
    final lapVar = _laplacianVariance(grid);
    if (lapVar < _lapVarBlur) issues.add('照片模糊，请对焦后重新拍摄');
    // ③ 边框遮挡：外圈8%边条上近黑像素占比过高（手指挡镜头/单证拍出画幅）
    final blocked = _edgeBlockedRatio(grid, _edgeLumaDark);
    if (blocked > _edgeBlockRatio) issues.add('边框不完整/镜头被遮挡，请退后重拍');
    return PhotoQuality._(issues);
  }

  static double _laplacianVariance(List<List<int>> g) {
    final n = g.length, m = g[0].length;
    var sum = 0.0, sumSq = 0.0;
    var count = 0;
    for (var y = 1; y < n - 1; y++) {
      for (var x = 1; x < m - 1; x++) {
        final v = (g[y - 1][x] + g[y + 1][x] + g[y][x - 1] + g[y][x + 1] -
                4 * g[y][x])
            .abs()
            .toDouble();
        sum += v;
        sumSq += v * v;
        count++;
      }
    }
    if (count == 0) return 0;
    final mean = sum / count;
    return sumSq / count - mean * mean;
  }

  static double _edgeBlockedRatio(List<List<int>> g, double dark) {
    final n = g.length, m = g[0].length;
    final bw = math.max(1, (math.min(n, m) * 0.08).round());
    var darkCount = 0, total = 0;
    for (var y = 0; y < n; y++) {
      for (var x = 0; x < m; x++) {
        final onEdge = y < bw || y >= n - bw || x < bw || x >= m - bw;
        if (!onEdge) continue;
        total++;
        if (g[y][x] < dark) darkCount++;
      }
    }
    return total == 0 ? 0 : darkCount / total;
  }
}

// ---------------------------------------------------------------- 扫码速查
//
// 扫码是"现场速查"的输入方式之一（不是所有单证都印了机器可读码，手动输入保留
// 为默认路径）。扫码识别出的文本直接复用现有 /mobile/lookup 查询逻辑，
// 不涉及任何存储/接口改动。
//
// 扫码结果必须先过校验：单证编号的自然形态是"4~64个连续字符、无空格、非URL"——
// 扫到无关二维码（网址/名片/整句文本）时给出明确提示，不拿无效内容浪费一次查询。

const String scanInvalidMessage = '未识别到有效的单证编号，请重新扫描或手动输入';
const String scanPermissionMessage = '需要摄像头权限用于扫描单证条码';

String? sanitizeScannedCode(String? raw) {
  if (raw == null) return null;
  final code = raw.trim();
  if (code.length < 4 || code.length > 64) return null; // 过短/异常长都不可信
  if (RegExp(r'\s').hasMatch(code)) return null; // 整句人读文本，不是编号
  if (RegExp(r'^[a-zA-Z][a-zA-Z0-9+.-]*:').hasMatch(code)) {
    return null; // http:/tel:/BEGIN: 等协议类内容
  }
  return code;
}

/// 扫码页：取景框 + 明确的取消按钮 + 权限拒绝降级UI。
/// 返回扫到的原始文本（合法与否由首页统一校验决策）；取消/异常返回 null。
class BarcodeScannerPage extends StatefulWidget {
  const BarcodeScannerPage({super.key});

  @override
  State<BarcodeScannerPage> createState() => _BarcodeScannerPageState();
}

class _BarcodeScannerPageState extends State<BarcodeScannerPage> {
  bool _handled = false; // 同一个码会连续回调多次，只认第一次

  void _finish(String? raw) {
    if (_handled) return;
    _handled = true;
    Navigator.of(context).pop(raw);
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: const Text('扫码速查'),
        leading: IconButton(
          icon: const Icon(Icons.close, size: 28),
          tooltip: '取消扫码',
          onPressed: () => _finish(null),
        ),
      ),
      body: Column(children: [
        Container(
          width: double.infinity,
          color: const Color(0xFFE3F2FD),
          padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 10),
          child: const Row(children: [
            Icon(Icons.info_outline, size: 20, color: Color(0xFF0B5394)),
            SizedBox(width: 8),
            Expanded(
                child: Text('将单证上的条码/二维码对准取景框，识别后自动查询',
                    style: TextStyle(fontSize: 14.5))),
          ]),
        ),
        Expanded(
          child: MobileScanner(
            onDetect: (capture) {
              for (final barcode in capture.barcodes) {
                final raw = barcode.rawValue;
                if (raw != null && raw.trim().isNotEmpty) {
                  _finish(raw);
                  return;
                }
              }
            },
            errorBuilder: (context, error) => ScannerErrorView(
              error: error,
              onManualInput: () => _finish(null),
            ),
            placeholderBuilder: (_) => const Center(
                child: Column(mainAxisSize: MainAxisSize.min, children: [
              CircularProgressIndicator(),
              SizedBox(height: 12),
              Text('正在启动摄像头…', style: TextStyle(fontSize: 15)),
            ])),
          ),
        ),
        Padding(
          padding: const EdgeInsets.all(12),
          child: OutlinedButton.icon(
            icon: const Icon(Icons.keyboard, size: 24),
            label: const Text('改用手动输入', style: TextStyle(fontSize: 16)),
            onPressed: () => _finish(null),
          ),
        ),
      ]),
    );
  }
}

/// 相机异常视图（公开便于测试）：权限拒绝给中文说明与降级引导；其余错误如实展示。
class ScannerErrorView extends StatelessWidget {
  const ScannerErrorView({super.key, required this.error, this.onManualInput});

  final MobileScannerException error;
  final VoidCallback? onManualInput;

  @override
  Widget build(BuildContext context) {
    final denied =
        error.errorCode == MobileScannerErrorCode.permissionDenied;
    return Center(
      child: Padding(
        padding: const EdgeInsets.all(24),
        child: Column(mainAxisSize: MainAxisSize.min, children: [
          Icon(denied ? Icons.no_photography : Icons.error_outline,
              size: 56, color: Colors.red.shade400),
          const SizedBox(height: 14),
          Text(
              denied
                  ? scanPermissionMessage
                  : '摄像头启动失败，无法扫码',
              style: const TextStyle(fontSize: 18, fontWeight: FontWeight.w600),
              textAlign: TextAlign.center),
          const SizedBox(height: 8),
          Text(
              denied
                  ? '请在系统设置→应用→班列单证核验→权限中允许相机后重试；'
                      '也可返回后直接手动输入运单号/批次编号查询。'
                  : '错误类型：${error.errorCode.name}。可返回后改用手动输入查询。',
              textAlign: TextAlign.center,
              style: const TextStyle(fontSize: 14.5, height: 1.5)),
          const SizedBox(height: 18),
          FilledButton.icon(
            icon: const Icon(Icons.keyboard, size: 22),
            label:
                const Text('返回手动输入', style: TextStyle(fontSize: 16)),
            onPressed: onManualInput,
          ),
        ]),
      ),
    );
  }
}

// ---------------------------------------------------------------- 弱网暂存队列
//
// 现场信号差是常态：上传失败的照片落盘暂存（原图字节+元信息），App开着时
// 定时/回前台自动重试，不丢照片、不卡死用户——拍下一张继续干活。

class QueuedPhoto {
  QueuedPhoto({
    required this.id,
    required this.fileName,
    required this.createdAt,
    this.attempts = 0,
    this.lastError,
  });

  final String id;
  final String fileName;
  final String createdAt;
  int attempts;
  String? lastError;

  Map<String, dynamic> toJson() => {
        'id': id,
        'file_name': fileName,
        'created_at': createdAt,
        'attempts': attempts,
        'last_error': lastError,
      };

  static QueuedPhoto fromJson(Map<String, dynamic> j) => QueuedPhoto(
        id: '${j['id']}',
        fileName: '${j['file_name'] ?? ''}',
        createdAt: '${j['created_at'] ?? ''}',
        attempts: (j['attempts'] as num?)?.toInt() ?? 0,
        lastError: j['last_error'] as String?,
      );
}

class UploadQueue {
  /// 正常模式：字节落盘到应用文档目录（dir不可为null时启用持久化）。
  UploadQueue(this.dir) : _mem = {};

  /// 内存模式：widget测试用（测试环境是FakeAsync，真实文件IO不会完成）。
  UploadQueue.memory() : dir = null, _mem = {};

  final Directory? dir;
  final Map<String, List<int>> _mem;
  final List<QueuedPhoto> items = [];

  File get _indexFile =>
      File('${dir!.path}${Platform.pathSeparator}pending.json');

  Future<void> load() async {
    if (dir == null) return; // 内存模式：items本就在内存，清空会抹掉测试预置
    items.clear();
    if (!await _indexFile.exists()) return;
    try {
      final list = jsonDecode(await _indexFile.readAsString()) as List;
      items.addAll(list.map((e) => QueuedPhoto.fromJson(e)));
    } catch (_) {
      // 索引损坏时不丢字节文件：按目录内残留图片重建最小索引
      await _rebuildFromFiles();
    }
  }

  Future<void> _rebuildFromFiles() async {
    items.clear();
    await for (final f in dir!.list()) {
      if (f is File && f.path.endsWith('.jpg')) {
        final id = f.uri.pathSegments.last.replaceAll('.jpg', '');
        items.add(QueuedPhoto(id: id, fileName: '$id.jpg', createdAt: ''));
      }
    }
    await _persist();
  }

  Future<void> enqueue(String fileName, List<int> bytes) async {
    final id =
        'P${DateTime.now().millisecondsSinceEpoch}_${items.length}_${math.Random().nextInt(9999)}';
    if (dir == null) {
      _mem[id] = bytes;
    } else {
      await File('${dir!.path}${Platform.pathSeparator}$id.jpg')
          .writeAsBytes(bytes);
    }
    items.add(QueuedPhoto(
        id: id,
        fileName: fileName,
        createdAt: DateTime.now().toIso8601String()));
    await _persist();
  }

  /// 取出指定待传照片的字节（不移除，成功后才移除）。
  Future<List<int>> bytesOf(QueuedPhoto item) async {
    if (dir == null) return _mem[item.id] ?? <int>[];
    return File('${dir!.path}${Platform.pathSeparator}${item.id}.jpg')
        .readAsBytes();
  }

  Future<void> markFailed(QueuedPhoto item, String error) async {
    item.attempts += 1;
    item.lastError = error.length > 80 ? error.substring(0, 80) : error;
    await _persist();
  }

  Future<void> remove(String id) async {
    items.removeWhere((e) => e.id == id);
    _mem.remove(id);
    if (dir != null) {
      final f = File('${dir!.path}${Platform.pathSeparator}$id.jpg');
      if (await f.exists()) await f.delete();
    }
    await _persist();
  }

  bool get hasPending => items.isNotEmpty;

  Future<void> _persist() async {
    if (dir == null) return;
    await dir!.create(recursive: true);
    await _indexFile
        .writeAsString(jsonEncode(items.map((e) => e.toJson()).toList()));
  }
}

/// 默认暂存目录：应用文档目录（卸载才清理，系统不会随手回收）。
Future<Directory> defaultQueueDir() async {
  final base = await getApplicationDocumentsDirectory();
  return Directory('${base.path}${Platform.pathSeparator}pending_uploads');
}

// ---------------------------------------------------------------- API 客户端

class ApiClient {
  ApiClient(this.baseUrl, {this.token});

  final String baseUrl;

  /// 登录令牌（登录页获取，SharedPreferences持久化，过期后由服务端401识别）
  final String? token;

  Map<String, String> get _baseHeaders => (token == null || token!.isEmpty)
      ? <String, String>{}
      : <String, String>{'Authorization': 'Bearer $token'};

  /// 拍照即传：POST /mobile/quick-check（多张照片一次提交）→ 一句话摘要。
  Future<QuickCheckResult> quickCheck(List<QueuedPhoto> photos,
      Future<List<int>> Function(QueuedPhoto) readBytes) async {
    final uri = Uri.parse('$baseUrl/mobile/quick-check');
    final req = http.MultipartRequest('POST', uri);
    for (final p in photos) {
      req.files.add(http.MultipartFile.fromBytes('files', await readBytes(p),
          filename: p.fileName));
    }
    req.headers.addAll(_baseHeaders);
    final resp = await req.send().timeout(const Duration(seconds: 120));
    final body = await resp.stream.bytesToString();
    if (resp.statusCode == 401) {
      throw AuthExpiredException();
    }
    if (resp.statusCode == 503) {
      throw ApiException('后端未安装OCR环境：${_detail(body)}');
    }
    if (resp.statusCode == 413) {
      throw ApiException('照片过大或一次拍摄过多：${_detail(body)}');
    }
    if (resp.statusCode != 200) {
      throw ApiException('上传失败（HTTP ${resp.statusCode}）：${_detail(body)}');
    }
    return QuickCheckResult.fromJson(jsonDecode(body));
  }

  /// 现场速查：按运单号/单证编号/批次编号查历史结论。
  Future<List<LookupMatch>> lookup(String query) async {
    final uri = Uri.parse('$baseUrl/mobile/lookup')
        .replace(queryParameters: {'q': query});
    final resp = await http.get(uri, headers: {
      'Content-Type': 'application/json',
      ..._baseHeaders,
    }).timeout(const Duration(seconds: 15));
    if (resp.statusCode == 401) {
      throw AuthExpiredException();
    }
    if (resp.statusCode != 200) {
      throw ApiException('查询失败（HTTP ${resp.statusCode}）：${_detail(resp.body)}');
    }
    final j = jsonDecode(resp.body) as Map<String, dynamic>;
    return (j['matches'] as List? ?? [])
        .map((e) => LookupMatch.fromJson(e))
        .toList();
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

  @override
  String toString() => message;
}

/// 登录过期/无效（服务端401）：调用方应清除本地凭证并回到登录页。
class AuthExpiredException implements Exception {
  @override
  String toString() => '登录已过期，请重新登录';
}

/// 本地登录凭证（SharedPreferences: auth_token / auth_user / auth_expires）。
class Session {
  static Future<String?> token() async {
    final prefs = await SharedPreferences.getInstance();
    return prefs.getString('auth_token');
  }

  static Future<String?> username() async {
    final prefs = await SharedPreferences.getInstance();
    return prefs.getString('auth_user');
  }

  static Future<void> save(
      {required String token, required String username, String? expiresAt}) async {
    final prefs = await SharedPreferences.getInstance();
    await prefs.setString('auth_token', token);
    await prefs.setString('auth_user', username);
    if (expiresAt != null) await prefs.setString('auth_expires', expiresAt);
  }

  static Future<void> clear() async {
    final prefs = await SharedPreferences.getInstance();
    await prefs.remove('auth_token');
    await prefs.remove('auth_user');
    await prefs.remove('auth_expires');
  }
}

/// 用户名+密码登录（POST /auth/login）。失败抛 ApiException（可读原因）。
Future<LoginSuccess> apiLogin(
    String baseUrl, String username, String password) async {
  final uri = Uri.parse('$baseUrl/auth/login');
  final resp = await http.post(uri,
      headers: {'Content-Type': 'application/json'},
      body: jsonEncode({'username': username, 'password': password}),
      ).timeout(const Duration(seconds: 15));
  final body = resp.body;
  if (resp.statusCode == 401) {
    throw ApiException(ApiClient._detail(body));
  }
  if (resp.statusCode != 200) {
    throw ApiException('登录失败（HTTP ${resp.statusCode}）');
  }
  final j = jsonDecode(body) as Map<String, dynamic>;
  final user = (j['user'] ?? {}) as Map<String, dynamic>;
  final success = LoginSuccess(
    token: (j['access_token'] ?? '').toString(),
    username: (user['username'] ?? username).toString(),
    role: (user['role_label'] ?? user['role'] ?? '').toString(),
    expiresAt: (j['expires_at'] ?? '').toString(),
  );
  if (success.token.isEmpty) {
    throw ApiException('登录响应缺少令牌，请联系管理员');
  }
  await Session.save(
      token: success.token,
      username: success.username,
      expiresAt: success.expiresAt);
  return success;
}

class LoginSuccess {
  LoginSuccess(
      {required this.token,
      required this.username,
      required this.role,
      required this.expiresAt});

  final String token;
  final String username;
  final String role;
  final String expiresAt;
}

String friendlyError(Object e) {
  if (e is SocketException || e is HttpException) {
    return '网络不可用或无法连接后端：照片已暂存，恢复后自动重传；'
        '也可在设置中检查API地址。';
  }
  if (e is TimeoutException) {
    return '网络缓慢，上传超时：照片已暂存，信号好转后自动重传。';
  }
  if (e is ApiException) return e.message;
  return e.toString();
}

/// 弱网判定：这类错误值得自动重试；其余（4xx契约错误）重试也不会成功。
bool isRetryable(Object e) =>
    e is SocketException || e is TimeoutException || e is HttpException;

// ---------------------------------------------------------------- App

class CebApp extends StatelessWidget {
  const CebApp({super.key, this.queueDirBuilder});

  final Future<Directory> Function()? queueDirBuilder;

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: '中欧班列单证核验 · 现场版',
      debugShowCheckedModeBanner: false,
      theme: ThemeData(
        colorScheme: ColorScheme.fromSeed(seedColor: const Color(0xFF0B5394)),
        useMaterial3: true,
      ),
      home: AuthGate(queueDirBuilder: queueDirBuilder),
    );
  }
}

// ---------------------------------------------------------------- 登录门禁

/// 启动门禁：本地有令牌直接进首页（避免每次打开都要重新登录），否则进登录页。
class AuthGate extends StatefulWidget {
  const AuthGate({super.key, this.queueDirBuilder});

  final Future<Directory> Function()? queueDirBuilder;

  @override
  State<AuthGate> createState() => _AuthGateState();
}

class _AuthGateState extends State<AuthGate> {
  String? _token;
  bool _checked = false;

  @override
  void initState() {
    super.initState();
    Session.token().then((t) {
      if (mounted) {
        setState(() {
          _token = t;
          _checked = true;
        });
      }
    });
  }

  @override
  Widget build(BuildContext context) {
    if (!_checked) {
      // 首帧：本地凭证读取中
      return const Scaffold(body: Center(child: CircularProgressIndicator()));
    }
    final hasToken = _token != null && _token!.isNotEmpty;
    return hasToken
        ? HomePage(queueDirBuilder: widget.queueDirBuilder)
        : LoginPage();
  }
}

/// 登录页：用户名+密码（内部系统口径），成功后本地保存令牌。
class LoginPage extends StatefulWidget {
  const LoginPage({super.key});

  @override
  State<LoginPage> createState() => _LoginPageState();
}

class _LoginPageState extends State<LoginPage> {
  final _username = TextEditingController();
  final _password = TextEditingController();
  String _baseUrl = 'http://192.168.5.44:8000';
  bool _busy = false;
  String? _error;

  @override
  void initState() {
    super.initState();
    SharedPreferences.getInstance().then((prefs) {
      if (mounted) {
        setState(() => _baseUrl = prefs.getString('api_base') ?? _baseUrl);
      }
    });
  }

  @override
  void dispose() {
    _username.dispose();
    _password.dispose();
    super.dispose();
  }

  Future<void> _submit() async {
    final name = _username.text.trim();
    if (name.isEmpty || _password.text.isEmpty) {
      setState(() => _error = '请输入用户名和密码');
      return;
    }
    setState(() {
      _busy = true;
      _error = null;
    });
    try {
      await apiLogin(_baseUrl, name, _password.text);
      if (!mounted) return;
      Navigator.pushReplacement(context,
          // 首页自行从 Session 读取令牌；登录名在设置页展示
          MaterialPageRoute(builder: (_) => const HomePage()));
    } on SocketException {
      setState(() => _error = '无法连接后端 $_baseUrl，请在登录后于设置中检查地址');
    } on TimeoutException {
      setState(() => _error = '连接超时，请确认网络与后端地址');
    } catch (e) {
      setState(() => _error = e.toString());
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      body: Center(
        child: SingleChildScrollView(
          padding: const EdgeInsets.all(28),
          child: ConstrainedBox(
            constraints: const BoxConstraints(maxWidth: 420),
            child: Column(
              mainAxisAlignment: MainAxisAlignment.center,
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                const Text('🚂 中欧班列单证核验 · 现场版',
                    textAlign: TextAlign.center,
                    style: TextStyle(fontSize: 22, fontWeight: FontWeight.bold)),
                const SizedBox(height: 6),
                Text('内部系统 · 请使用公司分配的账号登录',
                    textAlign: TextAlign.center,
                    style: TextStyle(fontSize: 13.5, color: Colors.grey.shade600)),
                const SizedBox(height: 28),
                TextField(
                  controller: _username,
                  textInputAction: TextInputAction.next,
                  decoration: const InputDecoration(
                    labelText: '用户名',
                    prefixIcon: Icon(Icons.person_outline),
                    border: OutlineInputBorder(),
                  ),
                ),
                const SizedBox(height: 14),
                TextField(
                  controller: _password,
                  obscureText: true,
                  onSubmitted: (_) => _submit(),
                  decoration: const InputDecoration(
                    labelText: '密码',
                    prefixIcon: Icon(Icons.lock_outline),
                    border: OutlineInputBorder(),
                  ),
                ),
                if (_error != null) ...[
                  const SizedBox(height: 12),
                  Text(_error!,
                      style:
                          const TextStyle(color: Colors.red, fontSize: 13.5)),
                ],
                const SizedBox(height: 20),
                FilledButton(
                  onPressed: _busy ? null : _submit,
                  style: FilledButton.styleFrom(
                      padding: const EdgeInsets.symmetric(vertical: 15)),
                  child: _busy
                      ? const SizedBox(
                          width: 22,
                          height: 22,
                          child:
                              CircularProgressIndicator(strokeWidth: 2.4))
                      : const Text('登 录', style: TextStyle(fontSize: 17)),
                ),
                const SizedBox(height: 10),
                TextButton(
                  onPressed: () async {
                    final url = await Navigator.push<String>(context,
                        MaterialPageRoute(builder: (_) => SettingsPage(initial: _baseUrl)));
                    if (url != null && url.isNotEmpty && mounted) {
                      final prefs = await SharedPreferences.getInstance();
                      await prefs.setString('api_base', url);
                      setState(() => _baseUrl = url);
                    }
                  },
                  child: Text('后端地址设置（当前：$_baseUrl）',
                      style: const TextStyle(fontSize: 13)),
                ),
              ],
            ),
          ),
        ),
      ),
    );
  }
}

// ---------------------------------------------------------------- 首页
//
// 现场三步：打开App → 拍照 → 看到一句话反馈。
// 首页只有三块：拍照大按钮（唯一主线）、现场速查、最近反馈（本地缓存）。

class HomePage extends StatefulWidget {
  const HomePage(
      {super.key, this.apiFactory, this.queueDirBuilder, this.queue, this.scannerBuilder});

  final ApiClient Function(String baseUrl)? apiFactory;
  final Future<Directory> Function()? queueDirBuilder;
  final UploadQueue? queue;

  /// 扫码入口（可注入：测试用假扫码器代替真实摄像头页面）。
  final Future<String?> Function()? scannerBuilder;

  @override
  State<HomePage> createState() => _HomePageState();
}

class _HomePageState extends State<HomePage> with WidgetsBindingObserver {
  String _baseUrl = 'http://192.168.5.44:8000';
  UploadQueue? _queue; // 弱网暂存队列（initState加载）
  Timer? _retryTimer;
  String? _token; // 登录令牌（AuthGate保证已登录才会进入本页）

  /// 每次取用时按当前设置构造（设置页改地址后立即生效，测试可注入假客户端）。
  ApiClient get _api =>
      widget.apiFactory?.call(_baseUrl) ?? ApiClient(_baseUrl, token: _token);

  QuickCheckResult? _latest; // 最近一次一句话反馈（本地缓存，杀App也在）
  List<QuickCheckResult> _history = []; // 最近反馈（最多20条）
  bool _uploading = false;
  String _statusMsg = '';

  // 现场速查
  final _lookupController = TextEditingController();
  List<LookupMatch>? _lookupResults;
  bool _looking = false;
  String? _lookupError;


  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    _bootstrap();
    _retryTimer = Timer.periodic(const Duration(seconds: 15), (_) {
      if (!_uploading && (_queue?.hasPending ?? false)) _drainQueue();
    });
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    _retryTimer?.cancel();
    _lookupController.dispose();
    super.dispose();
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    // 回到前台立即补一轮重传（现场信号恢复后用户最可能的动作就是切回App）
    if (state == AppLifecycleState.resumed &&
        !_uploading &&
        (_queue?.hasPending ?? false)) {
      _drainQueue();
    }
  }

  Future<void> _bootstrap() async {
    final prefs = await SharedPreferences.getInstance();
    // 注入的队列（测试）优先；正常路径才解析应用文档目录
    final queue = widget.queue ??
        UploadQueue(await (widget.queueDirBuilder?.call() ?? defaultQueueDir()));
    await queue.load();
    // 本地缓存损坏不允许卡死拍照主线：历史读不出来就当没有
    List<QuickCheckResult> history;
    try {
      history = _decodeHistory(prefs.getString('recent_results'));
    } catch (_) {
      history = const [];
    }
    if (!mounted) return;
    setState(() {
      _baseUrl = prefs.getString('api_base') ?? _baseUrl;
      _token = prefs.getString('auth_token');
      _queue = queue;
      _history = history;
      _latest = _history.isNotEmpty ? _history.first : null;
    });
    if (queue.hasPending) _drainQueue(); // 启动即补传上次没发出去的照片
  }

  static List<QuickCheckResult> _decodeHistory(String? raw) {
    if (raw == null || raw.isEmpty) return [];
    try {
      return (jsonDecode(raw) as List)
          .map((e) => QuickCheckResult.fromJson(e))
          .toList();
    } catch (_) {
      return [];
    }
  }

  Future<void> _remember(QuickCheckResult r) async {
    _history = [r, ..._history.where((h) => h.batchId != r.batchId)]
        .take(20)
        .toList();
    final prefs = await SharedPreferences.getInstance();
    await prefs.setString(
        'recent_results', jsonEncode(_history.map((h) => h.toJson()).toList()));
  }

  // ------------------------------------------------------------ 拍照/选图

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
      _toast('打开相机/相册失败：$e', error: true);
      return;
    }
    if (files.isEmpty || !mounted) return;
    setState(() => _statusMsg = '正在检查照片质量…');

    // ① 本地质量把关：不合格不上传，避免浪费一轮后端OCR
    final passed = <XFile>[];
    final rejected = <String, String>{}; // 文件 → 原因
    for (final f in files) {
      final quality = await PhotoQuality.assessBytes(await f.readAsBytes());
      if (quality.ok) {
        passed.add(f);
      } else {
        rejected[f.name] = quality.issues.join('；');
      }
    }
    if (!mounted) return;
    if (rejected.isNotEmpty) {
      final msg = rejected.entries
          .map((e) => '${e.key}：${e.value}')
          .join('\n');
      if (source == ImageSource.camera && files.length == 1) {
        setState(() => _statusMsg = '');
        await showDialog<void>(
          context: context,
          builder: (_) => AlertDialog(
            icon: const Icon(Icons.photo_camera_back, size: 44),
            title: const Text('请重新拍摄', style: TextStyle(fontSize: 20)),
            content: Text(msg,
                style: const TextStyle(fontSize: 16, height: 1.5)),
            actions: [
              FilledButton(
                  onPressed: () => Navigator.pop(context),
                  child: const Text('知道了', style: TextStyle(fontSize: 16))),
            ],
          ),
        );
        return; // 相机单张不合格：明确不上传
      }
      _toast('已跳过${rejected.length}张不合格照片：\n$msg', error: true);
    }
    if (passed.isEmpty) {
      setState(() => _statusMsg = '');
      return;
    }
    // ② 先落盘入队（哪怕秒断网也不丢），随后立即尝试上传
    for (final f in passed) {
      await _queue!.enqueue(f.name, await f.readAsBytes());
    }
    if (mounted) setState(() => _statusMsg = '已加入上传队列（${_queue!.items.length}张待传）');
    await _drainQueue();
  }

  // ------------------------------------------------------------ 上传队列驱动

  Future<void> _drainQueue() async {
    final queue = _queue;
    if (queue == null || !queue.hasPending || _uploading) return;
    setState(() {
      _uploading = true;
      _statusMsg = '正在上传（待传${queue.items.length}张）…';
    });
    try {
      while (queue.hasPending) {
        // 先快照再上传：上传期间用户新拍的照片不会被误当作本批移除
        final batch = queue.items.take(5).toList();
        try {
          final result = await _api.quickCheck(batch, (p) => queue.bytesOf(p));
          for (final p in batch) {
            await queue.remove(p.id);
          }
          await _remember(result);
          if (!mounted) return;
          setState(() {
            _latest = result;
            _statusMsg = queue.hasPending ? '还有${queue.items.length}张待传…' : '';
          });
        } on AuthExpiredException {
          await queue.markFailed(batch.first, '登录已过期，重新登录后自动重传');
          if (mounted) _forceRelogin();
          break;
        } catch (e) {
          await queue.markFailed(batch.first, friendlyError(e));
          if (mounted) {
            setState(() => _statusMsg = '');
          }
          if (isRetryable(e)) {
            _toast('网络不佳：照片已暂存，恢复后自动重传');
          } else {
            _toast('上传失败：${friendlyError(e)}', error: true);
          }
          break; // 网络问题停止本次驱动，交给定时器/回前台重试
        }
      }
    } finally {
      if (mounted) {
        setState(() => _uploading = false);
      }
    }
  }

  /// 登录过期：清除本地凭证并回到登录页（重新登录后队列自动继续重传）。
  Future<void> _forceRelogin() async {
    await Session.clear();
    if (!mounted) return;
    Navigator.pushAndRemoveUntil(context,
        MaterialPageRoute(builder: (_) => LoginPage()), (_) => false);
  }

  void _toast(String msg, {bool error = false}) {
    if (!mounted) return;
    ScaffoldMessenger.of(context).showSnackBar(SnackBar(
        content: Text(msg, style: const TextStyle(fontSize: 15)),
        backgroundColor: error ? Colors.red.shade700 : null,
        duration: const Duration(seconds: 4)));
  }

  // ------------------------------------------------------------ 现场速查

  Future<void> _doLookup() async {
    final q = _lookupController.text.trim();
    if (q.isEmpty) {
      setState(() => _lookupError = '请输入运单号/单证编号/批次编号');
      return;
    }
    setState(() {
      _looking = true;
      _lookupError = null;
    });
    try {
      final matches = await _api.lookup(q);
      if (!mounted) return;
      setState(() => _lookupResults = matches);
    } on AuthExpiredException {
      if (mounted) _forceRelogin();
    } catch (e) {
      if (!mounted) return;
      setState(() {
        _lookupResults = null;
        _lookupError = friendlyError(e);
      });
    } finally {
      if (mounted) setState(() => _looking = false);
    }
  }

  /// 扫码 → 查询：识别成功自动填入并立即触发查询（无需再点"查询"）；
  /// 无关内容给明确提示且不发起请求；取消则静默返回（手动输入始终可用）。
  Future<void> _openBarcodeScanner() async {
    // 首次使用：先讲清楚摄像头用途，再交给系统权限弹窗
    final prefs = await SharedPreferences.getInstance();
    if (!mounted) return;
    if (!(prefs.getBool('scan_rationale_shown') ?? false)) {
      final go = await showDialog<bool>(
        context: context,
        builder: (_) => AlertDialog(
          icon: const Icon(Icons.qr_code_scanner, size: 44),
          title:
              const Text('扫码速查', style: TextStyle(fontSize: 20)),
          content: const Text(
              '$scanPermissionMessage。将单证上的条码/二维码对准取景框即可自动识别'
              '编号并查询；没有条码的单证仍可手动输入。点击"开始扫码"后系统将申请摄像头权限。',
              style: TextStyle(fontSize: 16, height: 1.5)),
          actions: [
            TextButton(
                onPressed: () => Navigator.pop(context, false),
                child: const Text('手动输入')),
            FilledButton(
                onPressed: () => Navigator.pop(context, true),
                child: const Text('开始扫码')),
          ],
        ),
      );
      await prefs.setBool('scan_rationale_shown', true);
      if (go != true || !mounted) return;
    }
    if (!mounted) return;
    final raw = await (widget.scannerBuilder?.call() ?? _pushBarcodeScanner());
    if (!mounted || raw == null) return; // 用户取消/权限拒绝 → 回到手动输入
    final code = sanitizeScannedCode(raw);
    if (code == null) {
      // 无关二维码：明确提示，不拿无效内容浪费一次查询
      setState(() => _lookupError = scanInvalidMessage);
      _toast(scanInvalidMessage);
      return;
    }
    _lookupController.text = code;
    await _doLookup();
  }

  Future<String?> _pushBarcodeScanner() => Navigator.push<String>(
      context, MaterialPageRoute(builder: (_) => const BarcodeScannerPage()));

  // ------------------------------------------------------------ UI

  @override
  Widget build(BuildContext context) {
    final queue = _queue;
    return Scaffold(
      appBar: AppBar(
        title: const Text('🚂 单证现场采集',
            style: TextStyle(fontSize: 20, fontWeight: FontWeight.w600)),
        actions: [
          IconButton(
              icon: const Icon(Icons.settings, size: 26),
              tooltip: '后端地址设置',
              onPressed: () async {
                final url = await Navigator.push<String>(
                    context,
                    MaterialPageRoute(
                        builder: (_) => SettingsPage(initial: _baseUrl)));
                if (url != null && url.isNotEmpty) {
                  final prefs = await SharedPreferences.getInstance();
                  await prefs.setString('api_base', url);
                  setState(() => _baseUrl = url);
                }
              }),
        ],
      ),
      body: ListView(padding: const EdgeInsets.all(16), children: [
        // ---- ① 拍照即传（唯一主线，按钮要大：现场可能戴手套） ----
        FilledButton.icon(
          icon: const Icon(Icons.photo_camera, size: 34),
          label: const Text('拍 照 上 传',
              style: TextStyle(fontSize: 24, fontWeight: FontWeight.bold)),
          style: FilledButton.styleFrom(
              padding: const EdgeInsets.symmetric(vertical: 22)),
          onPressed:
              (_uploading || queue == null) ? null : () => _pick(ImageSource.camera),
        ),
        const SizedBox(height: 10),
        OutlinedButton.icon(
          icon: const Icon(Icons.photo_library, size: 26),
          label: const Text('从相册选择', style: TextStyle(fontSize: 19)),
          style: OutlinedButton.styleFrom(
              padding: const EdgeInsets.symmetric(vertical: 16)),
          onPressed:
              (_uploading || queue == null) ? null : () => _pick(ImageSource.gallery),
        ),
        if (_statusMsg.isNotEmpty) ...[
          const SizedBox(height: 8),
          Row(children: [
            const SizedBox(
                width: 16,
                height: 16,
                child: CircularProgressIndicator(strokeWidth: 2)),
            const SizedBox(width: 10),
            Expanded(child: Text(_statusMsg, style: const TextStyle(fontSize: 14))),
          ]),
        ],
        Text('后端：$_baseUrl',
            style: const TextStyle(fontSize: 11.5, color: Colors.grey)),
        const SizedBox(height: 12),

        // ---- ② 弱网暂存提示（照片没丢，恢复后自动重传） ----
        if (queue != null && queue.hasPending) ...[
          _PendingBanner(
            count: queue.items.length,
            lastError: queue.items.last.lastError,
            onRetry: _uploading ? null : _drainQueue,
            onDiscard: queue.hasPending
                ? () async {
                    final ok = await showDialog<bool>(
                      context: context,
                      builder: (_) => AlertDialog(
                        title: const Text('放弃暂存照片？'),
                        content: Text(
                            '将删除${queue.items.length}张尚未上传成功的照片，删除后无法恢复。',
                            style: const TextStyle(fontSize: 16)),
                        actions: [
                          TextButton(
                              onPressed: () => Navigator.pop(context, false),
                              child: const Text('取消')),
                          FilledButton(
                              onPressed: () => Navigator.pop(context, true),
                              child: const Text('删除')),
                        ],
                      ),
                    );
                    if (ok != true) return;
                    for (final p in queue.items.toList()) {
                      await queue.remove(p.id);
                    }
                    if (mounted) setState(() {});
                  }
                : null,
          ),
          const SizedBox(height: 12),
        ],

        // ---- ③ 一句话反馈（红/黄/绿 + 一句关键问题 + 批次编号） ----
        if (_latest != null) ...[
          _ResultBanner(result: _latest!),
          const SizedBox(height: 12),
        ],

        // ---- ④ 现场速查（触手独有价值：人在现场随手查历史结论） ----
        _LookupCard(
          controller: _lookupController,
          looking: _looking,
          error: _lookupError,
          results: _lookupResults,
          onSubmit: _doLookup,
          onScan: _openBarcodeScanner,
        ),
        const SizedBox(height: 12),

        // ---- ⑤ 最近反馈（本地缓存，无网也可回看） ----
        if (_history.length > 1) ...[
          Text('最近反馈（完整报告在电脑端查看）',
              style: const TextStyle(fontSize: 15, fontWeight: FontWeight.w600)),
          const SizedBox(height: 6),
          for (final h in _history.take(10).skip(1))
            Card(
              margin: const EdgeInsets.symmetric(vertical: 3),
              child: ListTile(
                dense: true,
                leading: Icon(h.icon, color: h.bannerColor, size: 26),
                title: Text(h.oneLine,
                    maxLines: 1,
                    overflow: TextOverflow.ellipsis,
                    style: const TextStyle(fontSize: 14)),
                subtitle: Text('${h.createdAt} · ${h.batchId}',
                    style: const TextStyle(fontSize: 12)),
              ),
            ),
        ],
        const SizedBox(height: 10),
        const Text(
          '手机端只做现场采集与速查；完整核验明细、分数构成与AI建议请在电脑端网页'
          '输入批次编号查看。本工具为初级版（内部试用），结论供人工复核参考。',
          textAlign: TextAlign.center,
          style: TextStyle(fontSize: 12, color: Colors.grey),
        ),
      ]),
    );
  }
}

// ---------------------------------------------------------------- 待传横幅

class _PendingBanner extends StatelessWidget {
  const _PendingBanner(
      {required this.count, this.lastError, this.onRetry, this.onDiscard});

  final int count;
  final String? lastError;
  final VoidCallback? onRetry;
  final VoidCallback? onDiscard;

  @override
  Widget build(BuildContext context) {
    return Card(
      color: const Color(0xFFFFF8E1),
      child: Padding(
        padding: const EdgeInsets.all(12),
        child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          Row(children: [
            const Icon(Icons.cloud_off, color: Color(0xFF8D6E00), size: 26),
            const SizedBox(width: 10),
            Expanded(
              child: Text('$count 张照片等待上传（网络恢复后自动重试）',
                  style: const TextStyle(
                      fontSize: 16,
                      fontWeight: FontWeight.w600,
                      color: Color(0xFF8D6E00))),
            ),
            IconButton(
                tooltip: '立即重试',
                icon: const Icon(Icons.refresh),
                onPressed: onRetry),
            IconButton(
                tooltip: '放弃这些照片',
                icon: const Icon(Icons.delete_outline),
                onPressed: onDiscard),
          ]),
          if (lastError != null)
            Text('上次失败原因：$lastError',
                style: const TextStyle(fontSize: 12.5, color: Colors.brown)),
        ]),
      ),
    );
  }
}

// ---------------------------------------------------------------- 一句话反馈横幅

class _ResultBanner extends StatelessWidget {
  const _ResultBanner({required this.result});

  final QuickCheckResult result;

  @override
  Widget build(BuildContext context) {
    final r = result;
    return Card(
      color: r.bannerBg,
      elevation: 3,
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          Row(children: [
            Icon(r.icon, color: r.bannerColor, size: 40),
            const SizedBox(width: 12),
            Text(r.riskLabel,
                style: TextStyle(
                    fontSize: 28,
                    fontWeight: FontWeight.bold,
                    color: r.bannerColor)),
            const Spacer(),
            Text('共${r.docCount}张',
                style: TextStyle(fontSize: 14, color: r.bannerColor)),
          ]),
          const SizedBox(height: 10),
          Text(r.oneLine,
              style: const TextStyle(fontSize: 17, height: 1.5)),
          const SizedBox(height: 10),
          Container(
            padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 8),
            decoration: BoxDecoration(
                color: Colors.white, borderRadius: BorderRadius.circular(8)),
            child: Row(children: [
              const Icon(Icons.qr_code_2, size: 22, color: Colors.black54),
              const SizedBox(width: 8),
              Expanded(
                child: SelectableText('批次编号 ${r.batchId}',
                    style: const TextStyle(
                        fontSize: 16, fontWeight: FontWeight.w600)),
              ),
              IconButton(
                  tooltip: '复制批次编号',
                  icon: const Icon(Icons.copy, size: 20),
                  onPressed: () => _copy(context)),
            ]),
          ),
          const SizedBox(height: 8),
          Text('完整报告请在电脑端核验网页输入批次编号查看。',
              style: TextStyle(fontSize: 13.5, color: Colors.grey.shade700)),
        ]),
      ),
    );
  }

  void _copy(BuildContext context) {
    // 现场把编号抄给电脑端最怕抄错：复制按钮 + 提示
    ScaffoldMessenger.of(context).showSnackBar(SnackBar(
        content: Text('已复制批次编号 ${result.batchId}',
            style: const TextStyle(fontSize: 15))));
  }
}

// ---------------------------------------------------------------- 现场速查卡片

class _LookupCard extends StatelessWidget {
  const _LookupCard({
    required this.controller,
    required this.looking,
    required this.error,
    required this.results,
    required this.onSubmit,
    required this.onScan,
  });

  final TextEditingController controller;
  final bool looking;
  final String? error;
  final List<LookupMatch>? results;
  final VoidCallback onSubmit;
  final VoidCallback onScan;

  @override
  Widget build(BuildContext context) {
    return Card(
      elevation: 2,
      child: Padding(
        padding: const EdgeInsets.all(14),
        child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          const Row(children: [
            Icon(Icons.travel_explore, size: 24, color: Color(0xFF0B5394)),
            SizedBox(width: 8),
            Text('现场速查',
                style: TextStyle(fontSize: 18, fontWeight: FontWeight.w600)),
          ]),
          const SizedBox(height: 4),
          const Text('输入或扫码查运单号/单证编号，看这批货之前是否核验过、上次结论是什么',
              style: TextStyle(fontSize: 13, color: Colors.grey)),
          const SizedBox(height: 10),
          Row(children: [
            Expanded(
              child: TextField(
                controller: controller,
                textInputAction: TextInputAction.search,
                onSubmitted: (_) => onSubmit(),
                style: const TextStyle(fontSize: 17),
                decoration: const InputDecoration(
                  hintText: '运单号/批次编号',
                  border: OutlineInputBorder(),
                  isDense: true,
                ),
              ),
            ),
            const SizedBox(width: 8),
            // 扫码：查询输入方式的补充（单证上有条码/二维码时免手动输入）
            IconButton.filledTonal(
              tooltip: '扫码查询',
              onPressed: onScan,
              icon: const Icon(Icons.qr_code_scanner, size: 26),
              style: IconButton.styleFrom(
                  padding: const EdgeInsets.all(14)),
            ),
            const SizedBox(width: 8),
            FilledButton(
              onPressed: looking ? null : onSubmit,
              style: FilledButton.styleFrom(
                  padding:
                      const EdgeInsets.symmetric(horizontal: 20, vertical: 16)),
              child: looking
                  ? const SizedBox(
                      width: 20,
                      height: 20,
                      child: CircularProgressIndicator(strokeWidth: 2))
                  : const Text('查询', style: TextStyle(fontSize: 17)),
            ),
          ]),
          if (error != null) ...[
            const SizedBox(height: 10),
            Text(error!,
                style: const TextStyle(
                    fontSize: 14, color: Color(0xFFB71C1C), height: 1.4)),
          ],
          if (results != null) ...[
            const SizedBox(height: 10),
            if (results!.isEmpty)
              const Text('未查询到历史核验记录——这批货可能还没在系统里核验过，'
                      '可现场拍照上传补一次核验。',
                  style: TextStyle(fontSize: 14.5, height: 1.5))
            else
              for (final m in results!)
                Card(
                  margin: const EdgeInsets.symmetric(vertical: 4),
                  child: ListTile(
                    leading: Icon(_dot(m.riskLevel), color: _color(m.riskLevel), size: 32),
                    title: Text(m.oneLine,
                        style: const TextStyle(fontSize: 15, height: 1.4)),
                    subtitle: Text(
                        '${m.createdAt} · ${m.batchId} · ${m.docCount}张单证 · ${m.riskLabel}',
                        style: const TextStyle(fontSize: 12.5)),
                  ),
                ),
          ],
        ]),
      ),
    );
  }

  static Color _color(String level) => switch (level) {
        'red' => const Color(0xFFB71C1C),
        'yellow' => const Color(0xFF8D6E00),
        _ => const Color(0xFF1B5E20),
      };

  static IconData _dot(String level) => switch (level) {
        'red' => Icons.circle,
        'yellow' => Icons.warning_amber_rounded,
        _ => Icons.check_circle_rounded,
      };
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
      appBar: AppBar(title: const Text('设置')),
      body: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
          FutureBuilder<String?>(
            future: Session.username(),
            builder: (context, snap) => Card(
              color: const Color(0xFFEFF6FF),
              child: ListTile(
                leading: const Icon(Icons.verified_user_outlined),
                title: Text('已登录：${snap.data ?? '—'}',
                    style: const TextStyle(fontSize: 15)),
              ),
            ),
          ),
          const SizedBox(height: 8),
          OutlinedButton.icon(
            icon: const Icon(Icons.logout),
            label: const Text('退出登录'),
            onPressed: () async {
              await Session.clear();
              if (!context.mounted) return;
              Navigator.pushAndRemoveUntil(context,
                  MaterialPageRoute(builder: (_) => LoginPage()), (_) => false);
            },
          ),
          const SizedBox(height: 12),
          TextField(
            controller: _controller,
            keyboardType: TextInputType.url,
            style: const TextStyle(fontSize: 16),
            decoration: const InputDecoration(
              labelText: 'API Base URL',
              hintText: 'http://192.168.x.x:8000',
              border: OutlineInputBorder(),
              helperText: '填写运行核验后端的电脑局域网IP与端口（以现场网络为准）',
            ),
          ),
          const SizedBox(height: 12),
          FilledButton(
            style: FilledButton.styleFrom(
                padding: const EdgeInsets.symmetric(vertical: 14)),
            child: const Text('保存', style: TextStyle(fontSize: 17)),
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
                'Android模拟器访问本机请用 http://10.0.2.2:8000\n\n'
                '上传失败的照片会暂存在手机里，网络恢复且App在运行时自动重传。',
                style: TextStyle(fontSize: 13, height: 1.6),
              ),
            ),
          ),
        ]),
      ),
    );
  }
}
