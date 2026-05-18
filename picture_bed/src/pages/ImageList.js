import React, { useState, useEffect } from 'react';
import { Upload, Card, Row, Col, Modal, message, Tooltip, Progress } from 'antd';
import { PlusOutlined, ShareAltOutlined, DeleteOutlined, DownloadOutlined, CheckCircleOutlined } from '@ant-design/icons';
import styled from '@emotion/styled';
import { useAuth } from '../contexts/AuthContext';  // 添加这行导入
import { fetchUserImages, uploadImage, deleteImage, shareFile, cancelShareFile, pvFile } from '../services/images';
import { describeFile } from '../services/ai';

const ImageGrid = styled(Row)`
  margin-top: 20px;
`;

const UploadCard = styled(Card)`
  width: 100%;
  height: 100%;
  .ant-upload-select {
    width: 100%;
    height: 200px;
  }
`;

const ImageCard = styled(Card)`
  margin-bottom: 16px;
  .ant-card-cover {
    height: 200px;
    overflow: hidden;
    display: flex;
    align-items: center;
    justify-content: center;
    img {
      max-width: 100%;
      max-height: 100%;
      object-fit: contain;
    }
  }
`;



const ImageList = () => {
  const [images, setImages] = useState([]);
  const [previewVisible, setPreviewVisible] = useState(false);
  const [previewImage, setPreviewImage] = useState('');
  const [previewTitle, setPreviewTitle] = useState('');
  const [uploading, setUploading] = useState(false);
  const [uploadProgress, setUploadProgress] = useState(0);
  const { user, logout } = useAuth();
  const uploadingRef = React.useRef(false);

  const isImageFile = (file) => {
    const imageTypes = ['png', 'jpg', 'jpeg', 'gif', 'bmp', 'webp', 'svg'];
    const type = (file.type || '').toLowerCase();
    return imageTypes.includes(type);
  };

  const fetchImages = async () => {
    try {
      const filesWithUrls = await fetchUserImages(user);
      setImages(filesWithUrls.filter(isImageFile));
    } catch (error) {
      console.error('获取图片列表错误：', error);
      if (error.tokenExpired) {
        message.error('登录已过期，请重新登录');
        logout();
        return;
      }
      message.error('获取图片列表失败，请检查网络连接');
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
        message.warning('图片已存在，无需重复上传');
      } else if (result.instant) {
        message.success('秒传成功！');
      } else {
        message.success('上传成功！');
      }
      // 异步调用 AI 生成描述（不阻塞上传流程）
      const apiKey = localStorage.getItem('dashscope_api_key_' + user.username) || '';
      describeFile(file, user, apiKey).catch(() => {});
      fetchImages();
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

  const handleDelete = async (image) => {
    try {
      await deleteImage(image, user);
      message.success('删除成功！');
      fetchImages();
    } catch (error) {
      console.error('删除错误：', error);
      message.error('删除失败！');
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
    setPreviewVisible(true);
  };

  const handleShare = async (image) => {
    try {
      await shareFile(image, user);
      message.success('分享成功！');
      fetchImages();
    } catch (error) {
      console.error('分享错误：', error);
      message.error('分享失败！');
    }
  };

  const handleCancelShare = async (image) => {
    try {
      await cancelShareFile(image, user);
      message.success('取消分享成功！');
      fetchImages();
    } catch (error) {
      console.error('取消分享错误：', error);
      message.error('取消分享失败！');
    }
  };

  // const handleDelete = async (image) => {
  //   try {
  //     console.log("image", image);
  //     const response = await fetch('http://192.168.88.124:8080/api/dealfile?cmd=del', {
  //       method: 'POST',
  //       headers: {
  //         'Content-Type': 'application/json'
  //       },
  //       body: JSON.stringify({
  //         token: user.token,
  //         user: user.username,
  //         md5: image.md5,
  //         filename: image.file_name
  //       })
  //     });
  
  //     const data = await response.json();
  //     if (data.code === 0) {
  //       message.success('删除成功！');
  //       fetchImages();  // 刷新列表
  //     } else {
  //       message.error(data.msg || '删除失败！');
  //     }
  //   } catch (error) {
  //     console.error('删除错误：', error);
  //     message.error('删除失败！');
  //   }
  // };

  const handleDownload = async (image) => {
    try {
      await pvFile(image, user);
    } catch (e) {
      // pv更新失败不影响下载
    }
    const link = document.createElement('a');
    link.href = image.url;
    link.download = image.name;
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
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
            >
              <p><PlusOutlined /></p>
              <p>点击或拖拽上传图片</p>
            </Upload.Dragger>
            {uploading && (
              <Progress percent={uploadProgress} status="active" style={{ marginTop: 12 }} />
            )}
          </UploadCard>
        </Col>
        {images.map(image => (
          <Col xs={24} sm={12} md={8} lg={6} key={image.id}>
            <ImageCard
              cover={<img alt={image.name} src={image.url} onClick={() => handlePreview(image)} />}
              actions={[
                image.share_status === 1 ? (
                  <Tooltip title="点击取消分享">
                    <CheckCircleOutlined style={{ color: '#52c41a' }} onClick={() => handleCancelShare(image)} />
                  </Tooltip>
                ) : (
                  <Tooltip title="分享">
                    <ShareAltOutlined onClick={() => handleShare(image)} />
                  </Tooltip>
                ),
                <Tooltip title="下载">
                  <DownloadOutlined onClick={() => handleDownload(image)} />
                </Tooltip>,
                <Tooltip title="删除">
                  <DeleteOutlined onClick={() => handleDelete(image)} />
                </Tooltip>,
              ]}
            >
              <Card.Meta title={image.name} description={`下载次数：${image.pv}`} />
            </ImageCard>
          </Col>
        ))}
      </Row>

      <Modal
        visible={previewVisible}
        title={previewTitle}
        footer={null}
        onCancel={() => setPreviewVisible(false)}
      >
        <img alt={previewTitle} style={{ width: '100%' }} src={previewImage} />
      </Modal>
    </div>
  );
};

export default ImageList;
