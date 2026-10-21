import { API_CONFIG } from '../config';

const BASE = API_CONFIG.BASE_URL;

/** 用户登录，成功返回 token */
export const loginUser = async (username, pwd) => {
  const resp = await fetch(`${BASE}${API_CONFIG.ENDPOINTS.LOGIN}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ user: username, pwd }),
  });
  const data = await resp.json();
  if (data.code !== 0 || !data.token) {
    throw new Error('用户名或密码错误');
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
  const data = await resp.json();
  if (data.code === 2) throw new Error('用户名已存在');
  if (data.code === 6) throw new Error('昵称已存在');
  if (data.code !== 0) throw new Error(data.msg || '注册失败');
};
