"use client";

import { forwardRef } from "react";
import type { ButtonHTMLAttributes } from "react";

import { cn } from "@/components/ui/cn";

/**
 * The shell's button, in the live product's two button species
 * (agent1-live-chrome/DOSSIER.md §G; agent2-code/CAPTURES.md cap-01):
 *
 * - `bevel` — the light-gray beveled "retro-OS" button used for every ordinary
 *   action (`EDIT`, `NEW TASKS`, `REFRESH`, `MANAGE DOMAIN`, …);
 * - `glossy` — the black glossy CTA, reserved for the one primary action on a
 *   surface (`SEE PLANS`, `RUN NOW`, `SEND`).
 *
 * The visual recipe lives in `globals.css` (`.btn`, `.btn-bevel`, `.btn-glossy`)
 * so non-React consumers — server components, plain HTML in U2 pages — get the
 * same buttons. Labels render in the mono ALL-CAPS voice via `.btn`; disabled
 * state is styled and never hidden.
 */
export type ButtonVariant = "bevel" | "glossy";
export type ButtonSize = "sm" | "md";

export type ButtonProps = ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: ButtonVariant;
  size?: ButtonSize;
};

const SIZE_CLASS: Record<ButtonSize, string> = {
  sm: "px-2 py-[3px] text-[10px]",
  md: "",
};

const VARIANT_CLASS: Record<ButtonVariant, string> = {
  bevel: "btn-bevel",
  glossy: "btn-glossy",
};

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button(
  { variant = "bevel", size = "md", type = "button", className, ...props },
  ref,
) {
  return (
    <button
      ref={ref}
      type={type}
      data-variant={variant}
      className={cn("btn", VARIANT_CLASS[variant], SIZE_CLASS[size], className)}
      {...props}
    />
  );
});
