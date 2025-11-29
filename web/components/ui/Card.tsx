import React from 'react';

interface CardProps {
  children: React.ReactNode;
  className?: string;
  hover?: boolean;
}

export const Card: React.FC<CardProps> = ({ children, className = '', hover = false }) => {
  return (
    <div
      className={`
        bg-card rounded-card border border-border shadow-sm
        ${hover ? 'hover:bg-card-hover transition-colors cursor-pointer' : ''}
        ${className}
      `}
    >
      {children}
    </div>
  );
};
