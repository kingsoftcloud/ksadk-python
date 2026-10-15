import { defineConfig } from '@playwright/test';
export default defineConfig({testDir:'./e2e',testMatch:'r85-network-consumers.spec.ts',workers:1,timeout:30000,use:{headless:true,channel:"chrome",viewport:{width:1440,height:1000},trace:'retain-on-failure',screenshot:'only-on-failure'}});
