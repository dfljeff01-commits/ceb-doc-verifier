// 照片质量检查测试（拍照即传主线的本地把关，纯网格数学，不依赖真实图片）。
//
// 阈值口径见 PhotoQuality 常量：宁可放过不可错杀（现场环境复杂，
// 误拦一张合格照片比放行一张模糊照片的现场代价更高）。
import 'dart:math' as math;

import 'package:ceb_verifier/main.dart';
import 'package:flutter_test/flutter_test.dart';

/// 生成 h×w 网格：均匀值（默认）或指定噪声幅度。
List<List<int>> grid(int h, int w, {int value = 180, int noise = 0, int seed = 7}) {
  final rng = math.Random(seed);
  return List.generate(
      h,
      (_) => List.generate(
          w, (_) => (value + (noise == 0 ? 0 : rng.nextInt(2 * noise) - noise))
              .clamp(0, 255)));
}

/// 在网格外圈画 bw 像素的近黑边框（模拟手指遮挡/单证出画幅）。
List<List<int>> withBlackFrame(List<List<int>> g, int bw) {
  final n = g.length, m = g[0].length;
  for (var y = 0; y < n; y++) {
    for (var x = 0; x < m; x++) {
      if (y < bw || y >= n - bw || x < bw || x >= m - bw) g[y][x] = 5;
    }
  }
  return g;
}

void main() {
  group('合格照片不误拦', () {
    test('明亮清晰的照片（带纹理噪声）全部通过', () {
      final q = PhotoQuality.assessGrid(grid(256, 192, value: 170, noise: 40),
          rawWidth: 3000, rawHeight: 4000);
      expect(q.ok, isTrue, reason: '不应有告警：${q.issues}');
    });
  });

  group('四项基础检查', () {
    test('均匀平坦画面判模糊（拉普拉斯方差为0）', () {
      final q = PhotoQuality.assessGrid(grid(256, 256, value: 200),
          rawWidth: 3000, rawHeight: 3000);
      expect(q.issues.join(), contains('模糊'));
    });

    test('平均亮度过低判过暗', () {
      final q = PhotoQuality.assessGrid(grid(256, 256, value: 20, noise: 12),
          rawWidth: 3000, rawHeight: 3000);
      expect(q.issues.join(), contains('光线过暗'));
      expect(q.issues.join(), isNot(contains('模糊')), reason: '噪声纹理清晰，不应误报模糊');
    });

    test('边缘近黑占比过高判遮挡/边框不完整', () {
      final q = PhotoQuality.assessGrid(
          withBlackFrame(grid(256, 256, value: 170, noise: 40), 20),
          rawWidth: 3000, rawHeight: 3000);
      expect(q.issues.join(), contains('边框不完整'));
    });

    test('原图最短边过低判分辨率不足', () {
      final q = PhotoQuality.assessGrid(grid(256, 256, value: 170, noise: 40),
          rawWidth: 640, rawHeight: 320);
      expect(q.issues.join(), contains('分辨率过低'));
    });
  });

  group('RGBA入口', () {
    test('RGBA字节按亮度加权正确缩略（纯色图=平坦→判模糊）', () {
      // 800x600 纯灰图 RGBA
      final rgba = List<int>.filled(800 * 600 * 4, 128);
      final q = PhotoQuality.assessRgba(rgba, 800, 600);
      expect(q.issues.join(), contains('模糊'));
    });

    test('解码失败给出可读原因而不是抛异常', () async {
      final q = await PhotoQuality.assessBytes(<int>[1, 2, 3, 4]);
      expect(q.ok, isFalse);
      expect(q.issues.join(), contains('无法读取'));
    });
  });
}
