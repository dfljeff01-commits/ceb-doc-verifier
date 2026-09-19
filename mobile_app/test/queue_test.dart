// 弱网暂存队列测试：照片先落盘再上传的可靠性契约——
// 失败不清队、字节不丢、重启（重新load）后队列还在。
import 'dart:io';

import 'package:ceb_verifier/main.dart';
import 'package:flutter_test/flutter_test.dart';

void main() {
  late Directory tmp;

  setUp(() async {
    tmp = await Directory.systemTemp.createTemp('upload_queue_test');
  });

  tearDown(() async {
    if (await tmp.exists()) await tmp.delete(recursive: true);
  });

  test('enqueue 落盘字节并持久化索引', () async {
    final q = UploadQueue(tmp);
    await q.enqueue('a.jpg', [1, 2, 3]);
    await q.enqueue('b.jpg', [4, 5]);
    expect(q.hasPending, isTrue);
    expect(q.items.length, 2);

    // 新实例（模拟App重启后）load 回来，队列不丢
    final q2 = UploadQueue(tmp);
    await q2.load();
    expect(q2.items.length, 2);
    expect(await q2.bytesOf(q2.items.first), [1, 2, 3]);
  });

  test('markFailed 保留照片并记录原因与次数', () async {
    final q = UploadQueue(tmp);
    await q.enqueue('a.jpg', List<int>.generate(32, (i) => i));
    final item = q.items.first;
    await q.markFailed(item, '网络不可用或无法连接后端');
    expect(q.hasPending, isTrue);
    expect(item.attempts, 1);
    expect(item.lastError, contains('网络不可用'));
    // 字节仍在盘上
    expect(await q.bytesOf(item).then((b) => b.length), 32);
    // 持久化的 attempts 也在
    final q2 = UploadQueue(tmp);
    await q2.load();
    expect(q2.items.first.attempts, 1);
  });

  test('remove 清除字节与索引', () async {
    final q = UploadQueue(tmp);
    await q.enqueue('a.jpg', [9, 9]);
    final id = q.items.first.id;
    await q.remove(id);
    expect(q.hasPending, isFalse);
    expect(File('${tmp.path}/$id.jpg').existsSync(), isFalse);
    final q2 = UploadQueue(tmp);
    await q2.load();
    expect(q2.items, isEmpty);
  });

  test('索引损坏时按残留字节文件重建，照片不丢', () async {
    final q = UploadQueue(tmp);
    await q.enqueue('keep.jpg', [7]);
    final id = q.items.first.id;
    // 故意写坏索引
    await File('${tmp.path}/pending.json').writeAsString('{broken json');
    final q2 = UploadQueue(tmp);
    await q2.load();
    expect(q2.items.map((e) => e.id), contains(id));
    expect(await q2.bytesOf(q2.items.first), [7]);
  });
}
