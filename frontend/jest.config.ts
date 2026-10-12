import type { Config } from "jest";

const config: Config = {
  testEnvironment: "jsdom",
  // Was `setupFilesAfterFramework`, which is not a Jest option (the real name is
  // `setupFilesAfterEnv`), so `src/__tests__/setup.ts` — the file that imports
  // `@testing-library/jest-dom` — was never loaded and every DOM matcher would have been missing.
  setupFilesAfterEnv: ["<rootDir>/src/__tests__/setup.ts"],
  moduleNameMapper: {
    "^@/(.*)$": "<rootDir>/src/$1",
  },
  transform: {
    "^.+\\.(ts|tsx)$": [
      "ts-jest",
      {
        tsconfig: {
          jsx: "react-jsx",
        },
      },
    ],
  },
  testMatch: ["**/__tests__/**/*.test.{ts,tsx}"],
  collectCoverageFrom: [
    "src/components/**/*.{ts,tsx}",
    "src/hooks/**/*.{ts,tsx}",
    "src/lib/**/*.{ts,tsx}",
    "!**/*.d.ts",
  ],
  coverageThreshold: {
    global: {
      branches: 60,
      functions: 70,
      lines: 70,
    },
  },
};

export default config;
