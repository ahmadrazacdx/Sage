import { Card, CardContent } from "@/components/ui/card";
import { AlertCircle } from "lucide-react";

export default function NotFound() {
  return (
    <div className="min-h-screen w-full flex items-center justify-center bg-background px-4">
      <Card className="w-full max-w-md border-sidebar-border bg-sidebar text-foreground shadow-xl">
        <CardContent className="pt-6">
          <div className="flex mb-4 gap-2 items-start">
            <AlertCircle className="h-8 w-8 text-warning" />
            <h1 className="text-2xl font-bold text-foreground">Page Not Found</h1>
          </div>

          <p className="mt-4 text-sm text-muted-foreground">
            The page you requested is not available. Return to the main workspace to continue using Sage.
          </p>

          <a
            href="/"
            className="mt-5 inline-flex items-center rounded-lg bg-primary px-3 py-2 text-sm font-medium text-primary-foreground hover:opacity-90 transition-opacity"
          >
            Go To Home
          </a>
        </CardContent>
      </Card>
    </div>
  );
}
