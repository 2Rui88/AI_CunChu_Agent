import { API_CONFIG } from '../config';

const BASE = API_CONFIG.BASE_URL;

/** 从 422 校验错误响应中提取可读信息 */
const extractValidationMsg = (data) => {
  const d = data && Array.isArray(data.detail) ? data.detail[0] : null;
  if (!d) return '';
  const field = Array.isArray(d.loc) ? d.loc[d.loc.length - 1] : '';
  const fieldNames = {
    username: '用户名（3-32位，仅字母/数字/下划线/连字符）',
    nick_name: '昵称（1-32个字符）',
    password: '密码（6-72个字符）',
    user: '用户名',
    pwd: '密码',
  };
  return `${fieldNames[field] || field || '输入'}格式不正确`;
};

/** 用户登录，成功返回 token */
export const loginUser = async (username, pwd) => {
  const resp = await fetch(`${BASE}${API_CONFIG.ENDPOINTS.LOGIN}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ user: username, pwd }),
  });
  const data = await resp.json().catch(() => ({}));
  if (data.code === 3) throw new Error('尝试过于频繁，请 5 分钟后再试');
  if (data.code !== 0 || !data.token) {
    throw new Error(resp.status === 422 ? (extractValidationMsg(data) || '输入格式不正确') : '用户名或密码错误');
  }
  return data.token;
};

/** 用户注册 */
export const registerUser = async ({ username, nick_name, password, phone = '', email = '' }) => {
  const resp = await fetch(`${BASE}${API_CONFIG.ENDPOINTS.REGISTER}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      username,
      nick_name,
      password,
      phone,
      email,
    }),
  });
  const data = await resp.json().catch(() => ({}));
  if (resp.status === 422) throw new Error(extractValidationMsg(data) || '注册信息格式不正确');
  if (data.code === 2) throw new Error('用户名已存在');
  if (data.code === 6) throw new Error('昵称已存在');
  if (data.code === 7) throw new Error('注册过于频繁，请 1 小时后再试');
  if (data.code !== 0) throw new Error(data.msg || '注册失败');
};
