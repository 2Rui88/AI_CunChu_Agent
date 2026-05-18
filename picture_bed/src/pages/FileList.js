import React, { useState, useEffect } from 'react';
import { Upload, Card, Row, Col, Table, message, Button, Tooltip, Space, Progress } from 'antd';
import {
  UploadOutlined,
  ShareAltOutlined,
  DeleteOutlined,
  DownloadOutlined,
  FileOutlined,
  FilePdfOutlined,
  FileWordOutlined,
  FileExcelOutlined,
  FileZipOutlined,
  FileTextOutlined,
  CheckCircleOutlined,
} from '@ant-design/icons';
import styled from '@emotion/styled';
import { useAuth } from '../contexts/AuthContext';
import { fetchUserImages, uploadImage, deleteImage, shareFile, cancelShareFile, pvFile } from '../services/images';
import { describeFile } from '../services/ai';

const UploadCard = styled(Card)`
  width: 100%;
  margin-bottom: 24px;
  .ant-upload-select {
    width: 100%;
    height: 160px;
  }
`;

const getFileIcon = (type) => {
  if (!type) return <FileOutlined />;
  const t = type.toLowerCase();
  if (t === 'pdf') return <FilePdfOutlined style={{ color: '#e53935' }} />;
  if (['doc', 'docx'].includes(t)) return <FileWordOutlined style={{ color: '#1976d2' }} />;
  if (['xls', 'xlsx'].includes(t)) return <FileExcelOutlined style={{ color: '#2e7d32' }} />;
  if (['zip', 'rar', '7z', 'tar', 'gz'].includes(t)) return <FileZipOutlined style={{ color: '#f57c00' }} />;
  if (['txt', 'md', 'log'].includes(t)) return <FileTextOutlined style={{ color: '#546e7a' }} />;
  return <FileOutlined />;
};

const FileList = () => {
  const [files, setFiles] = useState([]);
  const [uploading, setUploading] = useState(false);
  const [uploadProgress, setUploadProgress] = useState(0);
  const { user, logout } = useAuth();
  const uploadingRef = React.useRef(false);

  const fetchFiles = async () => {
    try {
      const data = await fetchUserImages(user);
      // 过滤出非图片文件
      const imageExts = ['png', 'jpg', 'jpeg', 'gif', 'bmp', 'webp', 'svg', 'ico'];
      const nonImageFiles = data.filter(f => {
        const ext = (f.type || '').toLowerCase();
        return !imageExts.includes(ext);
      });
      setFiles(nonImageFiles);
    } catch (error) {
      console.error('获取文件列表错误：', error);
      if (error.tokenExpired) {
        message.error('登录已过期，请重新登录');
        logout();
        return;
      }
      message.error('获取文件列表失败，请检查网络连接');
    }
  };

  const handleUpload = async (file) => {
    // 使用 ref 同步判断，防止异步状态更新导致的重复触发
    if (uploadingRef.current) return;
    uploadingRef.current = true;
    try {
      setUploading(true);
      setUploadProgress(0);
      const result = await uploadImage(file, user, (progress) => {
        setUploadProgress(progress);
      });
      if (result.alreadyExists) {
        message.warning('文件已存在，无需重复上传');
      } else if (result.instant) {
        message.success('秒传成功！');
      } else {
        message.success('上传成功！');
      }
      const apiKey = localStorage.getItem('dashscope_api_key_' + user.username) || '';
      describeFile(file, user, apiKey).catch(() => {});
      fetchFiles();
    } catch (error) {
      console.error('上传错误：', error);
      if (error.tokenExpired) {
        message.error('登录已过期，请重新登录');
        logout();
        return;
      }
      message.error('上传失败！');
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
      console.error('删除错误：', error);
      message.error('删除失败！');
    }
  };

  const handleShare = async (record) => {
    try {
      await shareFile(record, user);
      message.success('分享成功！');
      fetchFiles();
    } catch (error) {
      console.error('分享错误：', error);
      message.error('分享失败！');
    }
  };

  const handleCancelShare = async (record) => {
    try {
      await cancelShareFile(record, user);
      message.success('取消分享成功！');
      fetchFiles();
    } catch (error) {
      console.error('取消分享错误：', error);
      message.error('取消分享失败！');
    }
  };

  const handleDownload = async (record) => {
    try {
      await pvFile(record, user);
    } catch (e) {
      // pv更新失败不影响下载
    }
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

  const columns = [
    {
      title: '文件名',
      dataIndex: 'file_name',
      key: 'file_name',
      render: (text, record) => (
        <Space>
          {getFileIcon(record.type)}
          <span>{text}</span>
        </Space>
      ),
    },
    {
      title: '类型',
      dataIndex: 'type',
      key: 'type',
      width: 80,
      render: (t) => (t || '').toUpperCase(),
    },
    {
      title: '大小',
      dataIndex: 'size',
      key: 'size',
      width: 100,
      render: (size) => {
        if (!size) return '-';
        if (size < 1024) return `${size} B`;
        if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
        return `${(size / 1024 / 1024).toFixed(2)} MB`;
      },
    },
    {
      title: '上传时间',
      dataIndex: 'create_time',
      key: 'create_time',
      width: 180,
    },
    {
      title: '下载次数',
      dataIndex: 'pv',
      key: 'pv',
      width: 90,
    },
    {
      title: '操作',
      key: 'action',
      width: 160,
      render: (_, record) => (
        <Space>
          {record.share_status === 1 ? (
            <Tooltip title="点击取消分享">
              <Button type="text" icon={<CheckCircleOutlined style={{ color: '#52c41a' }} />} onClick={() => handleCancelShare(record)} />
            </Tooltip>
          ) : (
            <Tooltip title="分享">
              <Button type="text" icon={<ShareAltOutlined />} onClick={() => handleShare(record)} />
            </Tooltip>
          )}
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
      <UploadCard>
        <Upload.Dragger
          showUploadList={false}
          disabled={uploading}
          beforeUpload={(file) => {
            handleUpload(file);
            return false;
          }}
        >
          <p><UploadOutlined style={{ fontSize: 32, color: '#4CAF50' }} /></p>
          <p>点击或拖拽上传文件</p>
          <p style={{ color: '#999', fontSize: 12 }}>支持任意类型文件（图片请使用"上传图片"功能），大文件自动分片上传</p>
        </Upload.Dragger>
        {uploading && (
          <Progress percent={uploadProgress} status="active" style={{ marginTop: 12 }} />
        )}
      </UploadCard>

      <Table
        columns={columns}
        dataSource={files}
        rowKey="md5"
        pagination={{ pageSize: 10 }}
        locale={{ emptyText: '暂无文件，请上传' }}
      />
    </div>
  );
};

export default FileList;
