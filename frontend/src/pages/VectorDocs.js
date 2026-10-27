import React, { useState, useEffect, useMemo } from 'react';
import {
  Card, Table, message, Button, Tooltip, Space, Input, Empty, Drawer,
  Tabs, Tag, Descriptions, Spin, Typography,
} from 'antd';
import {
  SearchOutlined, ReloadOutlined, EyeOutlined, DatabaseOutlined,
} from '@ant-design/icons';
import styled from '@emotion/styled';
import { useAuth } from '../contexts/AuthContext';
import { fetchVectorDocs, fetchVectorChunks } from '../services/vectorDocs';
import { formatFileSize, getFileIcon } from '../utils/format';

const StatCard = styled(Card)`
  border-radius: 12px;
  text-align: center;
  .num {
    font-size: 22px;
    font-weight: 700;
    color: #2563eb;
  }
  .label {
    font-size: 12.5px;
    color: #8c95a3;
    margin-top: 2px;
  }
`;

const ListCard = styled(Card)`
  border-radius: 14px;
`;

const ChunkCard = styled.div`
  border: 1px solid #eef0f4;
  border-radius: 10px;
  padding: 12px 14px;
  margin-bottom: 12px;
  background: #fbfcfe;
`;

const ChunkMeta = styled.div`
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 8px;
  font-size: 12px;
  color: #8c95a3;
`;

const ChunkText = styled.div`
  white-space: pre-wrap;
  word-break: break-word;
  font-size: 13px;
  line-height: 1.8;
  color: #374151;
  max-height: 420px;
  overflow-y: auto;
`;

/** 文件级索引状态：全部块已入库 / 无块入库（存根）/ 部分入库（异常中间态） */
const docStatus = (d) => {
  if (!d.chunk_count) return { label: '无有效块', color: 'default' };
  if (d.embedded_count >= d.chunk_count) return { label: '已索引', color: 'green' };
  if (!d.embedded_count) return { label: '仅文件名索引', color: 'default' };
  return { label: '部分索引', color: 'orange' };
};

const VectorDocs = () => {
  const [docs, setDocs] = useState([]);
  const [totalVectors, setTotalVectors] = useState(0);
  const [loading, setLoading] = useState(false);
  const [filter, setFilter] = useState('');
  const [drawer, setDrawer] = useState({ open: false, loading: false, file: null, chunks: [] });
  const { user, logout } = useAuth();

  const fetchDocs = async () => {
    setLoading(true);
    try {
      const { docs: list, totalVectors: tv } = await fetchVectorDocs(user);
      setDocs(list);
      setTotalVectors(tv);
    } catch (error) {
      if (error.tokenExpired) {
        message.error('登录已过期，请重新登录');
        logout();
        return;
      }
      message.error(error.message || '获取向量化文档列表失败');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    if (user && user.token) {
      fetchDocs();
    }
  }, [user]);

  const openDoc = async (record) => {
    setDrawer({ open: true, loading: true, file: null, chunks: [] });
    try {
      const { file, chunks } = await fetchVectorChunks(user, record.md5);
      setDrawer({ open: true, loading: false, file, chunks });
    } catch (error) {
      if (error.tokenExpired) {
        message.error('登录已过期，请重新登录');
        logout();
        return;
      }
      message.error(error.message || '获取切分明细失败');
      setDrawer((d) => ({ ...d, loading: false }));
    }
  };

  const filtered = useMemo(
    () => docs.filter((d) => (d.file_name || '').toLowerCase().includes(filter.trim().toLowerCase())),
    [docs, filter],
  );

  const stubCount = useMemo(
    () => docs.filter((d) => !d.embedded_count).length,
    [docs],
  );
  const chunkTotal = useMemo(
    () => docs.reduce((acc, d) => acc + (Number(d.chunk_count) || 0), 0),
    [docs],
  );

  const columns = [
    {
      title: '文件名',
      dataIndex: 'file_name',
      key: 'file_name',
      render: (text, record) => (
        <Space>
          {getFileIcon(record.type)}
          <span style={{ fontWeight: 500 }}>{text || '(未命名)'}</span>
        </Space>
      ),
    },
    {
      title: '类型',
      dataIndex: 'type',
      key: 'type',
      width: 90,
      render: (t) => (t ? String(t).toUpperCase() : '-'),
    },
    {
      title: '大小',
      dataIndex: 'size',
      key: 'size',
      width: 110,
      render: (size) => formatFileSize(size),
    },
    {
      title: '切片数',
      dataIndex: 'chunk_count',
      key: 'chunk_count',
      width: 110,
      render: (val, record) => (
        <span>
          {record.embedded_count}/{val}
          <span style={{ color: '#b3bac4', fontSize: 12 }}> 已入库</span>
        </span>
      ),
    },
    {
      title: '索引状态',
      key: 'status',
      width: 130,
      render: (_, record) => {
        const s = docStatus(record);
        return <Tag color={s.color}>{s.label}</Tag>;
      },
    },
    {
      title: '更新时间',
      dataIndex: 'last_time',
      key: 'last_time',
      width: 170,
    },
    {
      title: '操作',
      key: 'action',
      width: 110,
      render: (_, record) => (
        <Tooltip title="查看切分明细">
          <Button
            type="link"
            size="small"
            icon={<EyeOutlined />}
            onClick={() => openDoc(record)}
          >
            查看切分
          </Button>
        </Tooltip>
      ),
    },
  ];

  const { file, chunks } = drawer;
  const embeddedTotal = (chunks || []).filter((c) => c.has_embedding).length;

  const chunkCards = (chunks || []).map((c) => (
    <ChunkCard key={c.chunk_index}>
      <ChunkMeta>
        <Tag color="blue" style={{ marginRight: 0 }}>#{c.chunk_index}</Tag>
        {c.context_label && <span>{c.context_label}</span>}
        <span>{c.char_len} 字</span>
        {c.has_embedding ? (
          <>
            <Tag color="green" style={{ marginRight: 0 }}>已向量化</Tag>
            <span>向量 #{c.faiss_id}</span>
          </>
        ) : (
          <Tag style={{ marginRight: 0 }}>未向量化</Tag>
        )}
      </ChunkMeta>
      <ChunkText>{c.description}</ChunkText>
    </ChunkCard>
  ));

  // 拼回全文：按顺序拼接所有块，角标 [N] 标记每块起点、交替底色显示切分边界
  const joinedView = (chunks || []).length > 0 && (
    <div style={{ whiteSpace: 'pre-wrap', wordBreak: 'break-word', fontSize: 13, lineHeight: 2, color: '#374151' }}>
      {chunks.map((c, i) => (
        <span
          key={c.chunk_index}
          style={{ background: i % 2 ? '#eff4ff' : 'transparent' }}
          title={`第 ${c.chunk_index} 块 · ${c.context_label || '无标签'} · ${c.char_len} 字`}
        >
          <sup style={{ color: '#94a3b8', fontSize: 10 }}>[{c.chunk_index}]</sup>
          {c.description}
        </span>
      ))}
    </div>
  );

  return (
    <div>
      <Space size={16} style={{ display: 'flex', marginBottom: 16 }} align="stretch">
        <StatCard style={{ flex: 1 }}><div className="num">{docs.length}</div><div className="label">向量化文档</div></StatCard>
        <StatCard style={{ flex: 1 }}><div className="num">{chunkTotal}</div><div className="label">切片总数</div></StatCard>
        <StatCard style={{ flex: 1 }}><div className="num">{totalVectors}</div><div className="label">入库向量块</div></StatCard>
        <StatCard style={{ flex: 1 }}><div className="num">{stubCount}</div><div className="label">仅文件名索引</div></StatCard>
      </Space>

      <ListCard>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 12 }}>
          <Space>
            <span style={{ fontWeight: 600 }}>已向量化文档</span>
            <Tooltip title="刷新列表">
              <Button type="text" icon={<ReloadOutlined />} onClick={fetchDocs} loading={loading} />
            </Tooltip>
          </Space>
          <Input
            allowClear
            size="middle"
            prefix={<SearchOutlined style={{ color: '#b3bac4' }} />}
            placeholder="按文件名筛选..."
            style={{ width: 240 }}
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
          />
        </div>
        <Table
          columns={columns}
          dataSource={filtered}
          rowKey="id"
          loading={loading}
          pagination={{ pageSize: 10, showTotal: (t) => `共 ${t} 个文档` }}
          locale={{
            emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无向量化文档，可在文件列表中建立内容索引" />,
          }}
        />
      </ListCard>

      <Drawer
        open={drawer.open}
        onClose={() => setDrawer({ open: false, loading: false, file: null, chunks: [] })}
        width={720}
        title={(
          <Space>
            <DatabaseOutlined style={{ color: '#2563eb' }} />
            <span>{(file && file.file_name) || '切分明细'}</span>
          </Space>
        )}
      >
        {drawer.loading ? (
          <div style={{ textAlign: 'center', padding: '80px 0' }}>
            <Spin tip="加载切分明细..." />
          </div>
        ) : (file && (
          <>
            <Descriptions
              size="small"
              column={2}
              bordered
              style={{ marginBottom: 16 }}
              items={[
                { key: 'name', label: '文件名', children: file.file_name || '-' },
                {
                  key: 'type', label: '类型',
                  children: file.type ? String(file.type).toUpperCase() : '-',
                },
                { key: 'size', label: '大小', children: formatFileSize(file.size) },
                {
                  key: 'chunks', label: '切片 / 入库',
                  children: `${chunks.length} / ${embeddedTotal}`,
                },
                {
                  key: 'model', label: '模型',
                  children: (chunks[0] && chunks[0].model) || '-',
                },
                {
                  key: 'md5', label: 'MD5',
                  children: (
                    <Typography.Text style={{ fontSize: 12 }} copyable ellipsis>
                      {file.md5}
                    </Typography.Text>
                  ),
                },
              ]}
            />
            <Tabs
              defaultActiveKey="chunks"
              items={[
                {
                  key: 'chunks',
                  label: `分块视图 (${chunks.length})`,
                  children: chunkCards.length
                    ? chunkCards
                    : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="无切块数据" />,
                },
                {
                  key: 'joined',
                  label: '拼回全文',
                  children: joinedView || (
                    <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="无内容" />
                  ),
                },
              ]}
            />
          </>
        ))}
      </Drawer>
    </div>
  );
};

export default VectorDocs;
