/**
 * 文件/图片服务 —— 上传（秒传 + 分片）、列表、分享、删除、转存
 *
 * 所有需要鉴权的请求都携带 user + token：
 *   - JSON 类接口放在 body
 *   - /api/upload 为 multipart，token 放在表单字段
 */
import { API_CONFIG } from '../config';
import SparkMD5 from 'spark-md5';

const BASE = API_CONFIG.BASE_URL;
const E = API_CONFIG.ENDPOINTS;

/** 统一 POST JSON，token 过期时抛出 tokenExpired 错误 */
const post = async (url, body) => {
  const resp = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  const data = await resp.json();
  if (data.code === 4) {
    const err = new Error('token expired');
    err.tokenExpired = true;
    throw err;
  }
  return data;
};

/** 分块流式计算文件 MD5，避免大文件一次性读入内存 */
const calculateMD5 = (file, onProgress) =>
  new Promise((resolve, reject) => {
    const chunkSize = 8 * 1024 * 1024;
    const spark = new SparkMD5.ArrayBuffer();
    let offset = 0;
    const reader = new FileReader();
    reader.onload = (e) => {
      spark.append(e.target.result);
      offset += e.target.result.byteLength;
      if (onProgress) {
        onProgress(Math.min(99, Math.round((offset / file.size) * 100)));
      }
      if (offset < file.size) {
        readNext();
      } else {
        resolve(spark.end());
      }
    };
    reader.onerror = () => reject(new Error('读取文件失败'));
    const readNext = () => reader.readAsArrayBuffer(file.slice(offset, offset + chunkSize));
    readNext();
  });

/**
 * 获取用户文件列表，字段规范化为页面所需格式
 * 返回: [{id, md5, name, file_name, url, size, type, pv, share_status, create_time}]
 */
export const fetchUserImages = async (user) => {
  const data = await post(`${BASE}${E.MY_FILES}?cmd=normal`, {
    user: user.username,
    token: user.token,
    cmd: 'normal',
    start: 0,
    count: 500,
  });
  if (data.code !== 0) throw new Error(data.msg || '获取文件列表失败');
  return (data.files || []).map((f, i) => ({
    ...f,
    id: `${f.md5}_${i}`,
    name: f.file_name,
    share_status: f.shared_status,
  }));
};

/** 大文件分片上传（断点续传） */
const uploadChunked = async (file, user, md5, onProgress) => {
  const chunkSize = API_CONFIG.CHUNK_SIZE;
  const chunkCount = Math.ceil(file.size / chunkSize) || 1;

  const init = await post(`${BASE}${E.CHUNK_INIT}`, {
    user: user.username,
    token: user.token,
    filename: file.name,
    md5,
    size: file.size,
    chunkCount,
  });
  if (init.code !== 0) throw new Error(init.msg || '分片初始化失败');

  // 断点续传：跳过已上传分片
  const uploaded = new Set((init.uploadedChunks || '').split(',').filter(Boolean));

  for (let i = 0; i < chunkCount; i++) {
    if (uploaded.has(String(i))) continue;
    const blob = file.slice(i * chunkSize, (i + 1) * chunkSize);
    const resp = await fetch(
      `${BASE}${E.CHUNK_UPLOAD}?md5=${md5}&index=${i}` +
      `&user=${encodeURIComponent(user.username)}&token=${encodeURIComponent(user.token)}`,
      {
        method: 'POST',
        body: blob,
      }
    );
    const data = await resp.json().catch(() => ({}));
    if (data.code === 4) {
      const err = new Error('token expired');
      err.tokenExpired = true;
      throw err;
    }
    if (data.code !== 0) throw new Error(data.msg || '分片上传失败');
    if (onProgress) onProgress(Math.round(((i + 1) / chunkCount) * 100));
  }

  const merge = await post(`${BASE}${E.CHUNK_MERGE}`, {
    user: user.username,
    token: user.token,
    md5,
    filename: file.name,
  });
  if (merge.code === 5) return { alreadyExists: true };
  if (merge.code !== 0) throw new Error(merge.msg || '分片合并失败');
  if (onProgress) onProgress(100);
  return {};
};

/**
 * 上传文件：秒传检测 → (大文件) 分片上传 / (小文件) 普通上传
 * 返回 {alreadyExists?} / {instant?} / {}
 */
export const uploadImage = async (file, user, onProgress) => {
  const md5 = await calculateMD5(file, onProgress);

  // 秒传检测：服务器已有相同内容则只添加引用
  const check = await post(`${BASE}${E.MD5}`, {
    user: user.username,
    token: user.token,
    md5,
    fileName: file.name,
  });
  if (check.code === 5) return { alreadyExists: true };
  if (check.code === 0) return { instant: true };

  if (file.size > API_CONFIG.CHUNK_THRESHOLD) {
    return uploadChunked(file, user, md5, onProgress);
  }

  const form = new FormData();
  form.append('file', file);
  form.append('user', user.username);
  form.append('token', user.token);
  form.append('md5', md5);

  const resp = await fetch(`${BASE}${E.UPLOAD}`, { method: 'POST', body: form });
  const data = await resp.json();
  if (data.code === 4) {
    const err = new Error('token expired');
    err.tokenExpired = true;
    throw err;
  }
  if (data.code === 5) return { alreadyExists: true };
  if (data.code !== 0) throw new Error(data.msg || '上传失败');
  if (onProgress) onProgress(100);
  return {};
};

/** dealfile 通用调用（share / del / pv） */
const dealFile = (cmd, file, user) =>
  post(`${BASE}${E.DEAL_FILE}?cmd=${cmd}`, {
    user: user.username,
    token: user.token,
    md5: file.md5,
    filename: file.file_name || file.name,
  });

export const deleteImage = async (file, user) => {
  const data = await dealFile('del', file, user);
  if (data.code !== 0) throw new Error(data.msg || '删除失败');
};

export const shareFile = async (file, user) => {
  const data = await dealFile('share', file, user);
  if (data.code === 3) throw new Error('该文件已被其他用户分享');
  if (data.code !== 0) throw new Error(data.msg || '分享失败');
};

export const pvFile = (file, user) => dealFile('pv', file, user);

export const cancelShareFile = async (file, user) => {
  const data = await post(`${BASE}${E.DEAL_SHARE_FILE}?cmd=cancel`, {
    user: user.username,
    token: user.token,
    md5: file.md5,
    filename: file.file_name || file.name,
  });
  if (data.code !== 0) throw new Error(data.msg || '取消分享失败');
};

/** 共享广场列表 */
export const fetchSharedFiles = async () => {
  const data = await post(`${BASE}${E.SHARE_FILES}?cmd=normal`, { cmd: 'normal' });
  if (data.code !== 0) throw new Error(data.msg || '获取共享列表失败');
  return data.files || [];
};

/** 转存共享文件到自己的空间 */
export const saveSharedFile = async (file, user) => {
  const data = await post(`${BASE}${E.DEAL_SHARE_FILE}?cmd=save`, {
    user: user.username,
    token: user.token,
    md5: file.md5,
    filename: file.file_name,
  });
  if (data.code === 5) throw new Error('已保存过该文件');
  if (data.code !== 0) throw new Error(data.msg || '转存失败');
};

/** 下载排行 */
export const fetchTopDownloads = async () => {
  const data = await post(`${BASE}${E.SHARE_FILES}?cmd=pvdesc`, { cmd: 'pvdesc' });
  if (data.code !== 0) throw new Error(data.msg || '获取下载排行失败');
  return data.files || [];
};
