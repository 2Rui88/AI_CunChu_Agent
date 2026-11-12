import React, { useState, useEffect, useMemo } from 'react';
import { Card, Table, Button, Tooltip, Space, message, Input, Select, Empty, Tag, Modal } from 'antd';
import { DeleteOutlined, ReloadOutlined, SearchOutlined } from '@ant-design/icons';
import styled from '@emotion/styled';
import { useAuth } from '../contexts/AuthContext';
import { fetchDeptFiles } from '../services/dept';
import { deleteDeptFile } from '../services/images';
import { getFileIcon, formatFileSize } from '../utils/format';
import UserAvatar from '../components/UserAvatar';

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

export default function DeptFiles() {
  const [files, setFiles] = useState([]);
  const [members, setMembers] = useState([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [member, setMember] = useState('');       // 空串 = 全部成员
  const [keyword, setKeyword] = useState('');
  const [deleting, setDeleting] = useState(null);
  const { user, logout } = useAuth();

  const load = async () => {
    setLoading(true);
    try {
      const data = await fetchDeptFiles(user, { member, keyword, count: 500 });
      setFiles(data.files || []);
      setMembers(data.members || []);
      setTotal(data.total || 0);
    } catch (error) {
      if (error.tokenExpired) {
        message.error('登录已过期，请重新登录');
        logout();
        return;
      }
      message.error(error.message || '获取部门文件失败');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    load();
    // member 变化时重新加载（keyword 走前端筛选，不触发请求）
  }, [member]);

  // 文件名筛选走前端（服务端一次取回本部门全量，部门内数据量可控）
  const filtered = useMemo(
    () => files.filter((f) => (f.file_name || '').toLowerCase().includes(keyword.trim().toLowerCase())),
    [files, keyword],
  );

  const handleDelete = (record) => {
    const isOthers = record.user !== user.username;
    Modal.confirm({
      title: `删除${isOthers ? `成员「${record.nick_name || record.user}」的` : '你的'}文件？`,
      content: `「${record.file_name}」将被删除${record.shared_status === 1 ? '（含共享记录）' : ''}，此操作不可恢复。`,
      okText: '删除',
      okButtonProps: { danger: true },
      cancelText: '取消',
      onOk: async () => {
        setDeleting(record.md5 + record.file_name);
        try {
          await deleteDeptFile(record, user);
          message.success(`已删除「${record.file_name}」`);
          await load();
        } catch (error) {
          if (error.tokenExpired) {
            message.error('登录已过期，请重新登录');
            logout();
            return;
          }
          message.error(error.message || '删除失败');
        } finally {
          setDeleting(null);
        }
      },
    });
  };

  const columns = [
    {
      title: '文件名',
      dataIndex: 'file_name',
      key: 'file_name',
      render: (text, record) => (
        <Space>
          {getFileIcon(record.type || (record.file_name || '').split('.').pop())}
          {record.url ? (
            <a href={record.url} target="_blank" rel="noreferrer" style={{ fontWeight: 500 }}>{text}</a>
          ) : (
            <span style={{ fontWeight: 500 }}>{text}</span>
          )}
          {record.shared_status === 1 && <Tag color="blue">已共享</Tag>}
        </Space>
      ),
    },
    {
      title: '所有者',
      dataIndex: 'user',
      key: 'user',
      width: 170,
      render: (name, record) => (
        <OwnerCell>
          <UserAvatar name={name} size={26} />
          <span>
            {record.nick_name || name}
            {name === user.username && <span style={{ color: '#8c95a3', fontSize: 12 }}>（我）</span>}
          </span>
        </OwnerCell>
      ),
    },
    { title: '大小', dataIndex: 'size', key: 'size', width: 100, render: (s) => formatFileSize(s) },
    {
      title: '上传时间',
      dataIndex: 'create_time',
      key: 'create_time',
      width: 180,
    },
    {
      title: '操作',
      key: 'action',
      width: 90,
      render: (_, record) => (
        <Tooltip title="删除">
          <Button
            type="text"
            danger
            icon={<DeleteOutlined />}
            loading={deleting === record.md5 + record.file_name}
            onClick={() => handleDelete(record)}
          />
        </Tooltip>
      ),
    },
  ];

  return (
    <WrapCard>
      <Toolbar>
        <span style={{ fontWeight: 600 }}>
          部门文件 <span style={{ fontWeight: 400, color: '#8c95a3', fontSize: 13 }}>（{total} 个文件）</span>
        </span>
        <Space wrap>
          <Select
            allowClear
            placeholder="全部成员"
            style={{ width: 160 }}
            value={member || undefined}
            onChange={(v) => setMember(v || '')}
            options={members.map((m) => ({
              value: m.username,
              label: `${m.nick_name || m.username}（${m.username}）`,
            }))}
          />
          <Input
            allowClear
            prefix={<SearchOutlined style={{ color: '#b3bac4' }} />}
            placeholder="按文件名筛选..."
            style={{ width: 200 }}
            value={keyword}
            onChange={(e) => setKeyword(e.target.value)}
          />
          <Button icon={<ReloadOutlined />} onClick={load} loading={loading}>
            刷新
          </Button>
        </Space>
      </Toolbar>

      <Table
        columns={columns}
        dataSource={filtered}
        rowKey={(r) => r.user + r.md5 + r.file_name}
        loading={loading}
        pagination={{ pageSize: 10, showTotal: (t) => `共 ${t} 个文件` }}
        locale={{
          emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="部门内暂无文件" />,
        }}
      />
    </WrapCard>
  );
}
