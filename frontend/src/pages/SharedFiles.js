import React, { useState, useEffect, useMemo } from 'react';
import { Card, Table, Button, Tooltip, Space, message, Avatar, Input, Empty } from 'antd';
import { SaveOutlined, DownloadOutlined, ReloadOutlined, SearchOutlined } from '@ant-design/icons';
import styled from '@emotion/styled';
import { useAuth } from '../contexts/AuthContext';
import { fetchSharedFiles, saveSharedFile, pvSharedFile } from '../services/images';
import { getFileIcon, formatFileSize } from '../utils/format';

const WrapCard = styled(Card)`
  border-radius: 14px;
`;

const Toolbar = styled.div`
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
  margin-bottom: 12px;
  flex-wrap: wrap;
`;

const OwnerCell = styled.div`
  display: flex;
  align-items: center;
  gap: 8px;
`;

const AVATAR_COLORS = ['#2563eb', '#7c3aed', '#0d9488', '#d97706', '#dc2626', '#475569'];

const ownerColor = (name) => {
  let h = 0;
  for (const ch of name || '') h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  return AVATAR_COLORS[h % AVATAR_COLORS.length];
};

export default function SharedFiles() {
  const [files, setFiles] = useState([]);
  const [loading, setLoading] = useState(false);
  const [filter, setFilter] = useState('');
  const [savingKey, setSavingKey] = useState(null);
  const { user, logout } = useAuth();

  const load = async () => {
    setLoading(true);
    try {
      setFiles(await fetchSharedFiles());
    } catch (error) {
      if (error.tokenExpired) {
        message.error('登录已过期，请重新登录');
        logout();
        return;
      }
      message.error(error.message || '获取共享列表失败');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    load();
  }, []);

  const handleSave = async (record) => {
    const key = record.md5 + record.file_name;
    setSavingKey(key);
    try {
      await saveSharedFile(record, user);
      message.success(`已转存「${record.file_name}」`);
    } catch (error) {
      if (error.tokenExpired) {
        message.error('登录已过期，请重新登录');
        logout();
        return;
      }
      message.error(error.message || '转存失败');
    } finally {
      setSavingKey(null);
    }
  };

  // 下载次数仅在共享广场下载时统计：上报计数成功后本地同步 +1，再打开文件
  const handleDownload = async (record) => {
    try {
      await pvSharedFile(record, user);
      setFiles((prev) => prev.map((f) => (
        f.md5 === record.md5 && f.file_name === record.file_name
          ? { ...f, pv: (f.pv || 0) + 1 }
          : f
      )));
    } catch (error) {
      if (error.tokenExpired) {
        message.error('登录已过期，请重新登录');
        logout();
        return;
      }
      // 计数失败不阻断下载
    }
    if (record.url) window.open(record.url, '_blank', 'noopener');
  };

  const filtered = useMemo(
    () => files.filter((f) => (f.file_name || '').toLowerCase().includes(filter.trim().toLowerCase())),
    [files, filter],
  );

  const columns = [
    {
      title: '文件名',
      dataIndex: 'file_name',
      key: 'file_name',
      render: (text, record) => (
        <Space>
          {getFileIcon(record.type || (record.file_name || '').split('.').pop())}
          <a href={record.url} target="_blank" rel="noreferrer" style={{ fontWeight: 500 }}>{text}</a>
        </Space>
      ),
    },
    {
      title: '分享者',
      dataIndex: 'user',
      key: 'user',
      width: 160,
      render: (name) => (
        <OwnerCell>
          <Avatar size={26} style={{ backgroundColor: ownerColor(name), fontSize: 12, fontWeight: 600 }}>
            {(name || '?').slice(0, 1).toUpperCase()}
          </Avatar>
          <span>{name}</span>
        </OwnerCell>
      ),
    },
    { title: '大小', dataIndex: 'size', key: 'size', width: 100, render: (s) => formatFileSize(s) },
    {
      title: '下载次数',
      dataIndex: 'pv',
      key: 'pv',
      width: 120,
      showSorterTooltip: false,
      sorter: (a, b) => (a.pv || 0) - (b.pv || 0),
    },
    { title: '分享时间', dataIndex: 'create_time', key: 'create_time', width: 180 },
    {
      title: '操作',
      key: 'action',
      width: 140,
      render: (_, record) => (
        <Space>
          <Tooltip title="下载">
            <Button type="text" icon={<DownloadOutlined />} onClick={() => handleDownload(record)} />
          </Tooltip>
          <Tooltip title="转存到我的文件">
            <Button
              type="text"
              icon={<SaveOutlined style={{ color: '#2563eb' }} />}
              loading={savingKey === record.md5 + record.file_name}
              onClick={() => handleSave(record)}
            />
          </Tooltip>
        </Space>
      ),
    },
  ];

  return (
    <WrapCard>
      <Toolbar>
        <span style={{ fontWeight: 600 }}>
          共享广场 <span style={{ fontWeight: 400, color: '#8c95a3', fontSize: 13 }}>（{files.length} 个文件）</span>
        </span>
        <Space>
          <Input
            allowClear
            prefix={<SearchOutlined style={{ color: '#b3bac4' }} />}
            placeholder="按文件名筛选..."
            style={{ width: 220 }}
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
          />
          <Button icon={<ReloadOutlined />} onClick={load} loading={loading}>
            刷新
          </Button>
        </Space>
      </Toolbar>

      <Table
        columns={columns}
        dataSource={filtered}
        rowKey={(r) => r.md5 + r.file_name}
        loading={loading}
        pagination={{ pageSize: 10, showTotal: (t) => `共 ${t} 个文件` }}
        locale={{
          emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无共享文件" />,
        }}
      />
    </WrapCard>
  );
}
