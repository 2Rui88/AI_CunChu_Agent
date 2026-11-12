import React from 'react';
import { Avatar } from 'antd';

/**
 * 成员头像 —— 每个成员一个稳定且互不相同的头像：
 * 用户名哈希 → 调色板取色 + 首字母，同一成员在全站（侧边栏/共享广场/部门文件）
 * 任何位置都是同一个头像，无需后端存储。
 */

// 20 色调色板（避开主题蓝与禁用灰，白字可读）
const AVATAR_COLORS = [
  '#2563eb', '#7c3aed', '#0d9488', '#d97706', '#dc2626',
  '#059669', '#9333ea', '#c026d3', '#ea580c', '#65a30d',
  '#0284c7', '#be123c', '#4f46e5', '#0f766e', '#b45309',
  '#7e22ce', '#15803d', '#e11d48', '#3730a3', '#a16207',
];

const hashCode = (name) => {
  let h = 0;
  for (const ch of String(name || '')) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  return h;
};

export const avatarColor = (name) => AVATAR_COLORS[hashCode(name) % AVATAR_COLORS.length];

const UserAvatar = ({ name, size = 26, style }) => (
  <Avatar
    size={size}
    style={{
      backgroundColor: avatarColor(name),
      fontSize: Math.round(size * 0.45),
      fontWeight: 600,
      flex: 'none',
      ...style,
    }}
  >
    {String(name || '?').slice(0, 1).toUpperCase()}
  </Avatar>
);

export default UserAvatar;
