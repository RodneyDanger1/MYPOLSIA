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

// Wave V7 primitives (seat S1) — consumers import only from "@/components/ui".
export { Switch, type SwitchProps } from "@/components/ui/Switch";
export { RadioList, type RadioListProps, type RadioOption } from "@/components/ui/RadioList";
export { Tabs, type TabsProps, type TabItem } from "@/components/ui/Tabs";
export { DataTable, type DataTableProps, type Column } from "@/components/ui/DataTable";
