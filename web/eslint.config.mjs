import { dirname } from "path";
import { fileURLToPath } from "url";
import { FlatCompat } from "@eslint/eslintrc";

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);

const compat = new FlatCompat({
  baseDirectory: __dirname,
});

const eslintConfig = [
  ...compat.extends("next/core-web-vitals", "next/typescript"),
  {
    // Identity is resolved once per request (perf T2-B, #505). Keep it that
    // way: no ad-hoc GoTrue round trips, no hand-built service-role clients.
    files: ["lib/**/*.ts", "lib/**/*.tsx", "app/**/*.ts", "app/**/*.tsx", "middleware.ts"],
    ignores: ["lib/supabase/service.ts", "lib/auth/identity.ts"],
    rules: {
      "no-restricted-syntax": [
        "error",
        {
          selector:
            "MemberExpression[object.object.name='process'][object.property.name='env'][property.name='SUPABASE_SERVICE_ROLE_KEY']",
          message:
            "Do not read SUPABASE_SERVICE_ROLE_KEY here; use createServiceClient() from '@/lib/supabase/service'.",
        },
        {
          selector:
            "CallExpression[callee.property.name='getUser'][callee.object.property.name='auth']",
          message:
            "Do not call auth.getUser() (a GoTrue round trip); use getRequestIdentity() / getRequestPrincipal() from '@/lib/auth/identity'.",
        },
      ],
    },
  },
];

export default eslintConfig;
