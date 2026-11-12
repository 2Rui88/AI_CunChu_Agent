import React, { createContext, useContext, useState, useCallback } from 'react';

const STORAGE_KEY = 'yuncunchu_user';
const AuthContext = createContext(null);

/** 从 localStorage 恢复登录态 */
const loadStoredUser = () => {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    const parsed = raw ? JSON.parse(raw) : null;
    return parsed && parsed.username && parsed.token ? parsed : null;
  } catch {
    return null;
  }
};

export const AuthProvider = ({ children }) => {
  const [user, setUser] = useState(loadStoredUser);

  // profile: { role, department } —— 部门权限隔离：角色决定菜单可见性（主管可见"部门文件"）
  const login = useCallback((username, token, profile = {}) => {
    const u = { username, token, role: profile.role || 'member', department: profile.department || '' };
    localStorage.setItem(STORAGE_KEY, JSON.stringify(u));
    // 清理旧版"用户自填 API Key"在本地的残留（密钥已改为服务端统一配置）
    Object.keys(localStorage)
      .filter((k) => k.startsWith('dashscope_api_key_'))
      .forEach((k) => localStorage.removeItem(k));
    setUser(u);
  }, []);

  const logout = useCallback(() => {
    localStorage.removeItem(STORAGE_KEY);
    setUser(null);
  }, []);

  return (
    <AuthContext.Provider value={{ user, login, logout }}>
      {children}
    </AuthContext.Provider>
  );
};

export const useAuth = () => useContext(AuthContext);
