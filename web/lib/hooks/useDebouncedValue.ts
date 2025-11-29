import { useEffect, useState } from 'react';

/**
 * Debounces a value and provides loading state to indicate when debouncing is active.
 * Useful for showing loading indicators while waiting for user input to stabilize.
 *
 * @param value - The value to debounce
 * @param delay - Delay in milliseconds before updating (default: 500ms)
 * @returns Object containing the debounced value and debouncing state
 *
 * @example
 * const [searchTerm, setSearchTerm] = useState('');
 * const { debouncedValue, isDebouncing } = useDebouncedValue(searchTerm, 500);
 *
 * return (
 *   <div>
 *     <input value={searchTerm} onChange={e => setSearchTerm(e.target.value)} />
 *     {isDebouncing && <Spinner />}
 *   </div>
 * );
 */
export function useDebouncedValue<T>(
  value: T,
  delay: number = 500
): { debouncedValue: T; isDebouncing: boolean } {
  const [debouncedValue, setDebouncedValue] = useState<T>(value);
  const [isDebouncing, setIsDebouncing] = useState<boolean>(false);

  useEffect(() => {
    // If the value changes, mark as debouncing
    if (value !== debouncedValue) {
      setIsDebouncing(true);
    }

    // Set up a timer to update the debounced value after the delay
    const timer = setTimeout(() => {
      setDebouncedValue(value);
      setIsDebouncing(false);
    }, delay);

    // Clean up the timer if value changes before delay expires
    return () => {
      clearTimeout(timer);
    };
  }, [value, delay, debouncedValue]);

  return { debouncedValue, isDebouncing };
}
