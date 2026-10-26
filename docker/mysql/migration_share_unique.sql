-- Prevent duplicate public shares after Redis loss or concurrent requests.
-- 存量库先去重（每组 md5+file_name 保留 id 最小的一条），否则唯一索引创建失败。
DELETE s1 FROM share_file_list s1
JOIN share_file_list s2
  ON  s1.md5 = s2.md5
  AND s1.file_name = s2.file_name
  AND s1.id > s2.id;

ALTER TABLE share_file_list
    ADD UNIQUE KEY uq_share_md5_filename (md5, file_name);

-- 分享计数起点与 Redis 对齐为 0（原 DEFAULT 1 会使重建后的排行榜多 1）
ALTER TABLE share_file_list MODIFY COLUMN pv INT DEFAULT 0;
