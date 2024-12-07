import { cn } from "#/utils/utils";
import { BetaBadge } from "./beta-badge";

interface NavTabProps {
  id: string;
  label: string;
  icon: React.ReactNode;
  isBeta?: boolean;
  isActive: boolean;
  onClick: (id: string) => void;
}

export function NavTab({ id, label, icon, isBeta, isActive, onClick }: NavTabProps) {
  return (
    <button
      onClick={() => onClick(id)}
      className={cn(
        "px-2 border-b border-r border-neutral-600 bg-root-primary flex-1",
        "first-of-type:rounded-tl-xl last-of-type:rounded-tr-xl last-of-type:border-r-0",
        "flex items-center gap-2",
        isActive && "bg-root-secondary",
      )}
    >
      {icon}
      {label}
      {isBeta && <BetaBadge />}
    </button>
  );
}
