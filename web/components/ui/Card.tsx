import React from 'react';

interface CardProps {
  children: React.ReactNode;
  className?: string;
  hover?: boolean;
  id?: string;
  onClick?: () => void;
}

export const Card: React.FC<CardProps> = ({ children, className = '', hover = false, id, onClick }) => {
  return (
    <div
      id={id}
      onClick={onClick}
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
