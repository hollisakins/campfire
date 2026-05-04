import React from 'react';

interface ErrorStateProps {
  message: string;
  className?: string;
}

export const ErrorState: React.FC<ErrorStateProps> = ({ message, className = '' }) => (
  <div
    className={`bg-red-50 dark:bg-red-950 border border-red-200 dark:border-red-900 rounded-lg p-4 ${className}`}
  >
    <p className="text-red-800 dark:text-red-400">{message}</p>
  </div>
);
