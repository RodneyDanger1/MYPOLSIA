/**
 * The shell's UI primitives (wave U1) — later waves import from here:
 *
 * ```tsx
 * import { Button, Card, Chip, SectionHeader } from "@/components/ui";
 * ```
 *
 * Every primitive is a thin React wrapper over the recipes in
 * `src/app/globals.css`, so the same look is available to server components and
 * plain class names without a React import.
 */
export { Button, type ButtonProps, type ButtonSize, type ButtonVariant } from "@/components/ui/Button";
export { Card, type CardProps } from "@/components/ui/Card";
export { Chip, type ChipProps, type ChipVariant } from "@/components/ui/Chip";
export { SectionHeader, type SectionHeaderProps } from "@/components/ui/SectionHeader";
export { cn } from "@/components/ui/cn";
