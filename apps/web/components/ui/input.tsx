import * as React from "react";
import { cn } from "@/lib/utils";

const Input = React.forwardRef<
  HTMLInputElement,
  React.InputHTMLAttributes<HTMLInputElement>
>(({ className, type = "text", ...props }, ref) => (
  <input
    ref={ref}
    type={type}
    className={cn(
      "flex h-9 w-full rounded-lg border border-[var(--border)] bg-[var(--surface)] px-3 py-1.5 text-base text-[var(--ink)] outline-none transition-colors md:text-[13px]",
      "placeholder:text-[var(--ink-faint)] hover:border-[var(--border-strong)] focus:border-kroma-500 focus-visible:ring-2 focus-visible:ring-kroma-500/30",
      "disabled:cursor-not-allowed disabled:opacity-50",
      className,
    )}
    {...props}
  />
));
Input.displayName = "Input";

export { Input };
