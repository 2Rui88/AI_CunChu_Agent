import React, { useState, useEffect, useMemo } from 'react';
import {
  Card, Table, message, Button, Tooltip, Space, Progress, Input, Empty, Upload,
} from 'antd';
import {
  UploadOutlined, ShareAltOutlined, DeleteOutlined, DownloadOutlined,
  CheckCircleOutlined, SearchOutlined,
} from '@ant-design/icons';
import styled from '@emotion/styled';
import { useAuth } from '../contexts/AuthContext';
import { fetchUserImages, uploadImage, deleteImage, shareFile, cancelShareFile, pvFile } from '../services/images';
import { describeFileByMd5 } from '../services/ai';
import { IMAGE_EXTS, formatFileSize, getFileIcon } from '../utils/format';

const UploadCard = styled(Card)`
  border-radius: 14px;
  border: 1px dashed #c9d4e6;
  background: #fbfcfe;
  margin-bottom: 16px;
  transition: border-color 0.2s, background 0.2s;
  &:hover {
    border-color: #2563eb;
    background: #f5f8ff;
  }
  .ant-upload-select {
    width: 100%;
  }
  .ant-upload.ant-upload-drag {
    border: none;
    background: transparent;
  }
`;

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

const FileList = () => {
  const [files, setFiles] = useState([]);
  const [filter, setFilter] = useState('');
  const [uploading, setUploading] = useState(false);
  const [uploadProgress, setUploadProgress] = useState(0);
  const { user, logout } = useAuth();
  const uploadingRef = React.useRef(false);

  const fetchFiles = async () => {
    try {
      const all = await fetchUserImages(user);
      setFiles(all.filter((f) => !IMAGE_EXTS.includes((f.type || '').toLowerCase())));
    } catch (error) {
      if (error.tokenExpired) {
        message.error('登录已过期，请重新登录');
        logout();
        return;
      }
      message.error('获取文件列表失败');
    }
  };

  const handleUpload = async (file) => {
    if (uploadingRef.current) return;
    uploadingRef.current = true;
    try {
      setUploading(true);
      setUploadProgress(0);
      const result = await uploadImage(file, user, (progress) => setUploadProgress(progress));
      if (result.alreadyExists) {
        message.warning('文件已存在，无需重复上传');
      } else if (result.instant) {
        message.success('秒传成功！');
      } else {
        message.success('上传成功！');
      }
      const apiKey = localStorage.getItem('dashscope_api_key_' + user.username) || '';
      if (result.md5) {
        const ext = (file.name.split('.').pop() || '').toLowerCase();
        describeFileByMd5(result.md5, file.name, ext, user, apiKey).catch(() => {});
      }
      fetchFiles();
    } catch (error) {
      if (error.tokenExpired) {
        message.error('登录已过期，请重新登录');
        logout();
        return;
      }
      message.error(error.message || '上传失败！');
    } finally {
      uploadingRef.current = false;
      setUploading(false);
      setUploadProgress(0);
    }
  };

  const handleDelete = async (record) => {
    try {
      await deleteImage(record, user);
      message.success('删除成功！');
      fetchFiles();
    } catch (error) {
      message.error(error.message || '删除失败！');
    }
  };

  const handleShare = async (record) => {
    try {
      await shareFile(record, user);
      message.success('分享成功！');
      fetchFiles();
    } catch (error) {
      message.error(error.message || '分享失败！');
    }
  };

  const handleCancelShare = async (record) => {
    try {
      await cancelShareFile(record, user);
      message.success('已取消分享');
      fetchFiles();
    } catch (error) {
      message.error(error.message || '取消分享失败！');
    }
  };

  const handleDownload = async (record) => {
    pvFile(record, user).catch(() => {});
    const link = document.createElement('a');
    link.href = record.url;
    link.download = record.name;
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
  };

  useEffect(() => {
    if (user && user.token) {
      fetchFiles();
    }
  }, [user]);

  const filtered = useMemo(
    () => files.filter((f) => (f.file_name || '').toLowerCase().includes(filter.trim().toLowerCase())),
    [files, filter],
  );

  const totalSize = useMemo(
    () => files.reduce((acc, f) => acc + (Number(f.size) || 0), 0),
    [files],
  );
  const sharedCount = files.filter((f) => f.share_status === 1).length;

  const columns = [
    {
      title: '文件名',
      dataIndex: 'file_name',
      key: 'file_name',
      render: (text, record) => (
        <Space>
          {getFileIcon(record.type)}
          <a href={record.url} target="_blank" rel="noreferrer" style={{ fontWeight: 500 }}>{text}</a>
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
      title: '上传时间',
      dataIndex: 'create_time',
      key: 'create_time',
      width: 170,
    },
    { title: '下载', dataIndex: 'pv', key: 'pv', width: 80 },
    {
      title: '操作',
      key: 'action',
      width: 170,
      render: (_, record) => (
        <Space>
          <Tooltip title={record.share_status === 1 ? '取消分享' : '分享到共享广场'}>
            <Button
              type="text"
              icon={record.share_status === 1
                ? <CheckCircleOutlined style={{ color: '#16a34a' }} />
                : <ShareAltOutlined />}
              onClick={() => (record.share_status === 1 ? handleCancelShare(record) : handleShare(record))}
            />
          </Tooltip>
          <Tooltip title="下载">
            <Button type="text" icon={<DownloadOutlined />} onClick={() => handleDownload(record)} />
          </Tooltip>
          <Tooltip title="删除">
            <Button type="text" danger icon={<DeleteOutlined />} onClick={() => handleDelete(record)} />
          </Tooltip>
        </Space>
      ),
    },
  ];

  return (
    <div>
      <Space size={16} style={{ display: 'flex', marginBottom: 16 }} align="stretch">
        <StatCard style={{ flex: 1 }}><div className="num">{files.length}</div><div className="label">文件总数</div></StatCard>
        <StatCard style={{ flex: 1 }}><div className="num">{formatFileSize(totalSize)}</div><div className="label">占用空间</div></StatCard>
        <StatCard style={{ flex: 1 }}><div className="num">{sharedCount}</div><div className="label">已分享</div></StatCard>
      </Space>

      <UploadCard>
        <Upload.Dragger
          showUploadList={false}
          disabled={uploading}
          beforeUpload={(file) => {
            handleUpload(file);
            return false;
          }}
        >
          <div style={{ padding: '18px 16px' }}>
            <UploadOutlined style={{ fontSize: 30, color: '#2563eb' }} />
            <p style={{ margin: '10px 0 2px', color: '#4b5563' }}>点击或拖拽上传文件</p>
            <p style={{ color: '#b3bac4', fontSize: 12, margin: 0 }}>
              支持任意类型 · 超过 10MB 自动分片上传、断点续传 · 上传后自动生成 AI 描述
            </p>
          </div>
        </Upload.Dragger>
        {uploading && (
          <div style={{ padding: '0 24px 14px' }}>
            <Progress percent={uploadProgress} status="active" size="small" />
          </div>
        )}
      </UploadCard>

      <ListCard>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 12 }}>
          <span style={{ fontWeight: 600 }}>我的文件</span>
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
          pagination={{ pageSize: 10, showTotal: (t) => `共 ${t} 个文件` }}
          locale={{
            emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无文件，拖拽到上方区域上传" />,
          }}
        />
      </ListCard>
    </div>
  );
};

export default FileList;
