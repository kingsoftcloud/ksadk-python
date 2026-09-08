import * as React from "react";
import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "@/lib/utils";

const badgeVariants = cva("inline-flex items-center rounded-full border px-2.5 py-0.5 text-xs font-semibold transition-colors", {
  variants: {
    variant: {
      default: "border-transparent bg-slate-900 text-slate-50",
      secondary: "border-transparent bg-slate-100 text-slate-900",
      success: "border-transparent bg-green-100 text-green-700",
      warning: "border-transparent bg-amber-100 text-amber-700",
      destructive: "border-transparent bg-red-100 text-red-700",
      outline: "text-slate-950",
    },
  },
  defaultVariants: { variant: "default" },
});

export interface BadgeProps extends React.HTMLAttributes<HTMLDivElement>, VariantProps<typeof badgeVariants> {}
export function Badge({ className, variant, ...props }: BadgeProps) {
  return <div className={cn(badgeVariants({ variant }), className)} {...props} />;
}
export { badgeVariants };
