import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

/**
 * Join conditional class names, letting later Tailwind utilities win over
 * earlier ones (`cn("btn", "px-4", "px-2")` → `btn px-2`). Every
 * `src/components/ui` primitive routes its `className` through this so callers
 * can override a primitive's own utilities without `!important`.
 */
export function cn(...inputs: ClassValue[]): string {
  return twMerge(clsx(inputs));
}
