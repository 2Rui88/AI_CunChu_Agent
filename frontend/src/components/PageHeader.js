import React from 'react';
import styled from '@emotion/styled';

const HeaderBar = styled.div`
  background: #fff;
  padding: 14px 24px;
  border-bottom: 1px solid #eef0f4;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  position: sticky;
  top: 0;
  z-index: 50;
`;

const Title = styled.h2`
  margin: 0;
  font-size: 17px;
  font-weight: 700;
  color: #1f2329;
`;

const Desc = styled.div`
  margin-top: 2px;
  font-size: 12.5px;
  color: #8c95a3;
`;

/** 页面顶栏：左侧标题 + 描述，右侧操作区 */
export default function PageHeader({ title, desc, extra }) {
  return (
    <HeaderBar>
      <div>
        <Title>{title}</Title>
        {desc && <Desc>{desc}</Desc>}
      </div>
      {extra && <div style={{ display: 'flex', gap: 8, flex: 'none' }}>{extra}</div>}
    </HeaderBar>
  );
}
