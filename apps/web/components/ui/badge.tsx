import * as React from "react";
import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "@/lib/utils";

const badgeVariants = cva(
  "inline-flex items-center gap-1 whitespace-nowrap rounded-md border px-1.5 py-0.5 text-[11px] font-medium [&_svg]:size-3 [&_svg]:shrink-0",
  {
    variants: {
      variant: {
        default:
          "border-[var(--border)] bg-[var(--surface-2)] text-[var(--ink-soft)]",
        primary:
          "border-kroma-500/40 bg-kroma-500/10 text-kroma-600 dark:text-kroma-300 font-semibold",
        success: "border-emerald-500/30 bg-emerald-500/10 text-emerald-600 dark:text-emerald-400",
        warning: "border-amber-500/30 bg-amber-500/10 text-amber-600 dark:text-amber-400",
        danger: "border-red-500/30 bg-red-500/10 text-red-600 dark:text-red-400",
        muted: "border-[var(--border)] text-[var(--ink-faint)] opacity-80",
      },
    },
    defaultVariants: { variant: "default" },
  },
);

export interface BadgeProps
  extends React.HTMLAttributes<HTMLSpanElement>,
    VariantProps<typeof badgeVariants> {}

function Badge({ className, variant, ...props }: BadgeProps) {
  return <span className={cn(badgeVariants({ variant }), className)} {...props} />;
}

export { Badge, badgeVariants };
