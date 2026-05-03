import React from 'react';
import { Loader2 } from 'lucide-react';

interface LoadingStateProps {
  label?: string;
  className?: string;
}

export const LoadingState: React.FC<LoadingStateProps> = ({
  label = 'Loading...',
  className = '',
}) => (
  <div className={`flex items-center justify-center py-16 ${className}`}>
    <Loader2 className="w-8 h-8 animate-spin text-primary" />
    <span className="ml-3 text-text-secondary dark:text-slate-400">{label}</span>
  </div>
);
