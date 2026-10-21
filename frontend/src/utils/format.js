import React from 'react';
import {
  FilePdfOutlined,
  FileWordOutlined,
  FileExcelOutlined,
  FileZipOutlined,
  FileTextOutlined,
  FileImageOutlined,
  FileOutlined,
} from '@ant-design/icons';

export const IMAGE_EXTS = ['png', 'jpg', 'jpeg', 'gif', 'bmp', 'webp', 'svg', 'ico'];

/** 文件大小格式化：1234567 -> "1.18 MB" */
export const formatFileSize = (size) => {
  const n = Number(size) || 0;
  if (!n) return '-';
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  if (n < 1024 * 1024 * 1024) return `${(n / 1024 / 1024).toFixed(2)} MB`;
  return `${(n / 1024 / 1024 / 1024).toFixed(2)} GB`;
};

/** 按扩展名返回文件类型图标 */
export const getFileIcon = (type, iconStyle) => {
  const style = { fontSize: 16, ...iconStyle };
  if (!type) return <FileOutlined style={style} />;
  const t = String(type).toLowerCase();
  if (t === 'pdf') return <FilePdfOutlined style={{ ...style, color: '#e53935' }} />;
  if (['doc', 'docx'].includes(t)) return <FileWordOutlined style={{ ...style, color: '#1976d2' }} />;
  if (['xls', 'xlsx', 'csv'].includes(t)) return <FileExcelOutlined style={{ ...style, color: '#2e7d32' }} />;
  if (['zip', 'rar', '7z', 'tar', 'gz'].includes(t)) return <FileZipOutlined style={{ ...style, color: '#f57c00' }} />;
  if (IMAGE_EXTS.includes(t)) return <FileImageOutlined style={{ ...style, color: '#0288d1' }} />;
  if (['txt', 'md', 'log', 'json', 'xml', 'html'].includes(t)) return <FileTextOutlined style={{ ...style, color: '#546e7a' }} />;
  return <FileOutlined style={style} />;
};
