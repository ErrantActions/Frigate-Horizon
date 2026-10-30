import { forwardRef } from "react";
import { LuPlus } from "react-icons/lu";
import { UpstreamLogo } from "../Logo";
import { cn } from "@/lib/utils";

type FrigatePlusIconProps = {
  className?: string;
  onClick?: () => void;
};

const FrigatePlusIcon = forwardRef<HTMLDivElement, FrigatePlusIconProps>(
  ({ className, onClick }, ref) => {
    return (
      <div
        ref={ref}
        className={cn("relative flex items-center", className)}
        onClick={onClick}
      >
        <UpstreamLogo className="size-full" />
        <LuPlus className="absolute size-2 translate-x-3 translate-y-3/4" />
      </div>
    );
  },
);

export default FrigatePlusIcon;
