import React from 'react';

interface CardProps {
  children: React.ReactNode;
  className?: string;
  hover?: boolean;
  id?: string;
}

export const Card: React.FC<CardProps> = ({ children, className = '', hover = false, id }) => {
  return (
    <div
      id={id}
      className={`
        bg-card dark:bg-slate-800 rounded-card border border-border dark:border-slate-700 shadow-sm
        ${hover ? 'hover:bg-card-hover dark:hover:bg-slate-700 transition-colors cursor-pointer' : ''}
        ${className}
      `}
    >
      {children}
    </div>
  );
};
