/**
 * 向量化文档只读校验服务 —— 文档级聚合列表 + 单文档切分明细
 */
import { API_CONFIG } from '../config';

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

/**
 * 已向量化文档列表（每文件一行聚合）
 * 返回: { docs: [{id, md5, file_name, type, size, chunk_count,
 *                  embedded_count, stub_count, last_time}],
 *         total, totalVectors }
 */
export const fetchVectorDocs = async (user) => {
  const data = await post(`${BASE}${E.VECTOR_DOCS}`, {
    user: user.username,
    token: user.token,
    start: 0,
    count: 500,
  });
  if (data.code !== 0) throw new Error(data.msg || '获取向量化文档列表失败');
  return {
    docs: (data.docs || []).map((d, i) => ({ ...d, id: `${d.md5}_${i}` })),
    total: data.total || 0,
    totalVectors: data.total_vectors || 0,
  };
};

/** 单个文档的全部切分明细，返回 { file, chunks } */
export const fetchVectorChunks = async (user, md5) => {
  const data = await post(`${BASE}${E.VECTOR_CHUNKS}`, {
    user: user.username,
    token: user.token,
    md5,
  });
  if (data.code !== 0) throw new Error(data.msg || '获取切分明细失败');
  return { file: data.file || {}, chunks: data.chunks || [] };
};
