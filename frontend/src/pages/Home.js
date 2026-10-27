import React from 'react';
import { Card } from 'antd';
import styled from '@emotion/styled';
import AssistantChat from '../components/AssistantChat';

const HeroCard = styled(Card)`
  border-radius: 16px;
  background: linear-gradient(135deg, #eff6ff 0%, #f5f3ff 60%, #ffffff 100%);
  border: 1px solid #e3eaf6;
  text-align: center;
  padding-top: 12px;
  margin-bottom: 20px;
`;

const HeroTitle = styled.h2`
  font-size: 24px;
  font-weight: 700;
  color: #1f2329;
  margin: 8px 0 6px;
`;

const HeroDesc = styled.div`
  color: #6b7280;
  font-size: 14px;
  margin-bottom: 8px;
`;

/**
 * 首页 —— 与 AI 文件助手对话
 * 助手是唯一的文件搜索入口（也负责管理操作），
 * 独立的语义搜索框已移除，找文件直接在对话里说。
 */
export default function Home() {
  return (
    <div>
      <HeroCard>
        <HeroTitle>和 AI 助手对话，搜文件 · 管文件</HeroTitle>
        <HeroDesc>
          直接说"帮我找上个月的技术方案"、"一张有猫的照片"、"统计我的存储用量"、"把最新文件分享到共享广场"
        </HeroDesc>
      </HeroCard>

      <AssistantChat />
    </div>
  );
}
