import React from 'react';

interface BadgeProps {
  value: string | number;
  label: string;
  className?: string;
  compact?: boolean;
}

export const Badge: React.FC<BadgeProps> = ({ value, label, className = '', compact = false }) => {
  return (
    <div
      className={`
        bg-card border border-border rounded-xl
        flex flex-col items-center justify-center
        ${compact ? 'px-3 py-2' : 'px-6 py-4'}
        ${className}
      `}
    >
      <div className={`font-bold text-text-primary ${compact ? 'text-xl' : 'text-3xl'}`}>
        {value}
      </div>
      <div className={`font-medium text-text-secondary uppercase tracking-wide ${compact ? 'text-[10px] mt-0.5' : 'text-xs mt-1'}`}>
        {label}
      </div>
    </div>
  );
};
