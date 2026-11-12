/**
 * 部门文件服务 —— 主管查看本部门全部成员的文件（含私有）
 * 删除操作复用 images.js 的 deleteDeptFile（dealfile cmd=del + owner）
 */
import { API_CONFIG } from '../config';

const BASE = API_CONFIG.BASE_URL;
const E = API_CONFIG.ENDPOINTS;

const post = async (url, body) => {
  const resp = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  let data;
  try {
    data = await resp.json();
  } catch {
    throw new Error(`服务响应异常 (HTTP ${resp.status})`);
  }
  if (data.code === 4) {
    const err = new Error('登录已过期');
    err.tokenExpired = true;
    throw err;
  }
  return data;
};

/**
 * 部门文件列表（主管专属）
 * params: { member, keyword, start, count }
 * 返回 { files, total, members }
 */
export const fetchDeptFiles = async (user, params = {}) => {
  const data = await post(`${BASE}${E.DEPT_FILES}`, {
    user: user.username,
    token: user.token,
    member: params.member || '',
    keyword: params.keyword || '',
    start: params.start || 0,
    count: params.count || 500,
  });
  if (data.code === 6) throw new Error(data.msg || '仅部门主管可访问');
  if (data.code !== 0) throw new Error(data.msg || '获取部门文件失败');
  return data;
};
