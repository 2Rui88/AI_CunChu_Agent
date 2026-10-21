import React, { useState, useEffect } from 'react';
import { Upload, Card, Row, Col, Modal, message, Tooltip, Progress, Button, Image } from 'antd';
import {
  PlusOutlined, ShareAltOutlined, DeleteOutlined, DownloadOutlined, CheckCircleOutlined, EyeOutlined,
} from '@ant-design/icons';
import styled from '@emotion/styled';
import { useAuth } from '../contexts/AuthContext';
import { fetchUserImages, uploadImage, deleteImage, shareFile, cancelShareFile, pvFile } from '../services/images';
import { describeFile } from '../services/ai';
import { IMAGE_EXTS } from '../utils/format';

const UploadCard = styled(Card)`
  border-radius: 14px;
  border: 1px dashed #c9d4e6;
  background: #fbfcfe;
  display: flex;
  align-items: center;
  justify-content: center;
  height: 230px;
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

const GalleryCard = styled(Card)`
  border-radius: 14px;
  overflow: hidden;
  border: 1px solid #eef0f4;
  transition: box-shadow 0.2s, transform 0.2s;
  &:hover {
    box-shadow: 0 6px 20px rgba(15, 23, 42, 0.09);
    transform: translateY(-2px);
  }
  .ant-card-body {
    padding: 12px;
  }
`;

const ImageWrap = styled.div`
  position: relative;
  height: 170px;
  border-radius: 10px;
  overflow: hidden;
  cursor: pointer;
  background: #f2f4f8;
  img {
    width: 100%;
    height: 100%;
    object-fit: cover;
    transition: transform 0.3s;
  }
  &:hover img {
    transform: scale(1.06);
  }
  .overlay {
    position: absolute;
    inset: 0;
    background: rgba(15, 23, 42, 0.45);
    opacity: 0;
    transition: opacity 0.25s;
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 10px;
  }
  &:hover .overlay {
    opacity: 1;
  }
  .shared-badge {
    position: absolute;
    top: 8px;
    right: 8px;
    background: rgba(22, 163, 74, 0.9);
    color: #fff;
    font-size: 11px;
    padding: 2px 8px;
    border-radius: 999px;
    display: flex;
    align-items: center;
    gap: 4px;
  }
`;

const ImageMeta = styled.div`
  margin-top: 10px;
  .name {
    font-size: 13px;
    font-weight: 600;
    color: #1f2329;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .pv {
    font-size: 12px;
    color: #8c95a3;
    margin-top: 2px;
  }
`;

const ActionBtn = styled(Button)`
  background: rgba(255, 255, 255, 0.92) !important;
  border: none !important;
  box-shadow: 0 2px 6px rgba(0, 0, 0, 0.18);
`;

const ImageList = () => {
  const [images, setImages] = useState([]);
  const [previewOpen, setPreviewOpen] = useState(false);
  const [previewImage, setPreviewImage] = useState('');
  const [previewTitle, setPreviewTitle] = useState('');
  const [uploading, setUploading] = useState(false);
  const [uploadProgress, setUploadProgress] = useState(0);
  const { user, logout } = useAuth();
  const uploadingRef = React.useRef(false);
  // 预签名 URL 过期自愈：图片加载失败且距上次刷新超过 5 分钟时重拉列表换新签名
  const urlStaleRef = React.useRef(0);

  const isImageFile = (file) => IMAGE_EXTS.includes((file.type || '').toLowerCase());

  const fetchImages = async () => {
    try {
      const all = await fetchUserImages(user);
      setImages(all.filter(isImageFile));
    } catch (error) {
      if (error.tokenExpired) {
        message.error('登录已过期，请重新登录');
        logout();
        return;
      }
      message.error('获取图片列表失败');
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
        message.warning('图片已存在，无需重复上传');
      } else if (result.instant) {
        message.success('秒传成功！');
      } else {
        message.success('上传成功！');
      }
      const apiKey = localStorage.getItem('dashscope_api_key_' + user.username) || '';
      describeFile(file, user, apiKey).catch(() => {});
      fetchImages();
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

  const handleDelete = async (image) => {
    try {
      await deleteImage(image, user);
      message.success('删除成功！');
      fetchImages();
    } catch (error) {
      message.error(error.message || '删除失败！');
    }
  };

  useEffect(() => {
    if (user && user.token) {
      fetchImages();
    }
  }, [user]);

  const handlePreview = (file) => {
    setPreviewImage(file.url);
    setPreviewTitle(file.name);
    setPreviewOpen(true);
  };

  const handleShare = async (image) => {
    try {
      await shareFile(image, user);
      message.success('分享成功！');
      fetchImages();
    } catch (error) {
      message.error(error.message || '分享失败！');
    }
  };

  const handleCancelShare = async (image) => {
    try {
      await cancelShareFile(image, user);
      message.success('已取消分享');
      fetchImages();
    } catch (error) {
      message.error(error.message || '取消分享失败！');
    }
  };

  const handleDownload = async (image) => {
    pvFile(image, user).catch(() => {});
    const link = document.createElement('a');
    link.href = image.url;
    link.download = image.name;
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
  };

  const handleImgError = () => {
    const now = Date.now();
    if (now - urlStaleRef.current > 5 * 60 * 1000) {
      urlStaleRef.current = now;
      fetchImages();
    }
  };

  return (
    <div>
      <Row gutter={[16, 16]}>
        <Col xs={24} sm={12} md={8} lg={6}>
          <UploadCard>
            <Upload.Dragger
              accept="image/*"
              showUploadList={false}
              disabled={uploading}
              beforeUpload={(file) => {
                handleUpload(file);
                return false;
              }}
              style={{ background: 'transparent' }}
            >
              <div style={{ padding: '28px 16px' }}>
                <PlusOutlined style={{ fontSize: 34, color: '#2563eb' }} />
                <p style={{ marginTop: 12, color: '#4b5563' }}>点击或拖拽上传图片</p>
                <p style={{ color: '#b3bac4', fontSize: 12 }}>自动生成 AI 描述，可语义搜索</p>
              </div>
            </Upload.Dragger>
            {uploading && (
              <div style={{ padding: '0 20px 16px', width: '100%' }}>
                <Progress percent={uploadProgress} status="active" size="small" />
              </div>
            )}
          </UploadCard>
        </Col>

        {images.map((image) => (
          <Col xs={24} sm={12} md={8} lg={6} key={image.id}>
            <GalleryCard>
              <ImageWrap>
                <img src={image.url} alt={image.name} onError={handleImgError} onClick={() => handlePreview(image)} />
                {image.share_status === 1 && (
                  <span className="shared-badge"><CheckCircleOutlined /> 已分享</span>
                )}
                <div className="overlay" onClick={(e) => e.stopPropagation()}>
                  <Tooltip title="预览">
                    <ActionBtn shape="circle" icon={<EyeOutlined />} onClick={() => handlePreview(image)} />
                  </Tooltip>
                  <Tooltip title={image.share_status === 1 ? '取消分享' : '分享'}>
                    <ActionBtn
                      shape="circle"
                      icon={image.share_status === 1
                        ? <CheckCircleOutlined style={{ color: '#16a34a' }} />
                        : <ShareAltOutlined />}
                      onClick={() => (image.share_status === 1 ? handleCancelShare(image) : handleShare(image))}
                    />
                  </Tooltip>
                  <Tooltip title="下载">
                    <ActionBtn shape="circle" icon={<DownloadOutlined />} onClick={() => handleDownload(image)} />
                  </Tooltip>
                  <Tooltip title="删除">
                    <ActionBtn
                      shape="circle"
                      danger
                      icon={<DeleteOutlined style={{ color: '#ef4444' }} />}
                      onClick={() => handleDelete(image)}
                    />
                  </Tooltip>
                </div>
              </ImageWrap>
              <ImageMeta>
                <div className="name" title={image.name}>{image.name}</div>
                <div className="pv">下载 {image.pv || 0} 次</div>
              </ImageMeta>
            </GalleryCard>
          </Col>
        ))}
      </Row>

      {images.length === 0 && !uploading && (
        <Card style={{ borderRadius: 14, border: 'none' }}>
          <div style={{ textAlign: 'center', padding: '32px 0', color: '#8c95a3' }}>
            还没有图片，点击左上角区域上传第一张吧
          </div>
        </Card>
      )}

      <Modal
        open={previewOpen}
        title={previewTitle}
        footer={null}
        onCancel={() => setPreviewOpen(false)}
        centered
        width={720}
      >
        <Image
          src={previewImage}
          alt={previewTitle}
          style={{ width: '100%', borderRadius: 8, marginTop: 8 }}
          preview={false}
        />
      </Modal>
    </div>
  );
};

export default ImageList;
