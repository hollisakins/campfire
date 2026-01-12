import React from 'react';

interface ButtonProps extends React.ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: 'primary' | 'secondary' | 'ghost';
  size?: 'sm' | 'md' | 'lg';
  children: React.ReactNode;
}

export const Button: React.FC<ButtonProps> = ({
  variant = 'primary',
  size = 'md',
  children,
  className = '',
  disabled,
  ...props
}) => {
  const baseClasses = 'inline-flex items-center justify-center whitespace-nowrap rounded-lg font-medium transition-colors focus:outline-none focus:ring-2 focus:ring-offset-2 dark:focus:ring-offset-slate-900';

  const variantClasses = {
    primary: 'bg-primary hover:bg-primary-hover text-white focus:ring-primary',
    secondary: 'border-2 border-border dark:border-slate-600 hover:border-text-secondary dark:hover:border-slate-500 text-text-primary dark:text-slate-100 bg-background dark:bg-slate-800',
    ghost: 'text-text-primary dark:text-slate-100 hover:bg-card dark:hover:bg-slate-700',
  };

  const disabledClasses = 'opacity-50 cursor-not-allowed pointer-events-none';

  const sizeClasses = {
    sm: 'px-3 py-1.5 text-sm',
    md: 'px-4 py-2 text-base',
    lg: 'px-6 py-3 text-lg',
  };

  return (
    <button
      className={`${baseClasses} ${variantClasses[variant]} ${sizeClasses[size]} ${disabled ? disabledClasses : ''} ${className}`}
      disabled={disabled}
      {...props}
    >
      {children}
    </button>
  );
};
