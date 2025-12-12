import { useEffect, useState, useRef } from 'react';

/**
 * Compares two values for equality, using JSON comparison for objects.
 */
function isEqual<T>(a: T, b: T): boolean {
  if (typeof a === 'object' && a !== null && typeof b === 'object' && b !== null) {
    return JSON.stringify(a) === JSON.stringify(b);
  }
  return a === b;
}

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
  const timeoutRef = useRef<NodeJS.Timeout | null>(null);

  useEffect(() => {
    // Use deep equality for objects, reference equality for primitives
    const hasChanged = !isEqual(value, debouncedValue);

    if (hasChanged) {
      setIsDebouncing(true);
    }

    // Clear any existing timeout
    if (timeoutRef.current) {
      clearTimeout(timeoutRef.current);
    }

    // Set up a timer to update the debounced value after the delay
    timeoutRef.current = setTimeout(() => {
      setDebouncedValue(value);
      setIsDebouncing(false);
    }, delay);

    // Clean up the timer on unmount or value change
    return () => {
      if (timeoutRef.current) {
        clearTimeout(timeoutRef.current);
      }
    };
  }, [value, delay]); // Removed debouncedValue from dependencies to prevent re-triggering loops

  return { debouncedValue, isDebouncing };
}
