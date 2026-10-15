import React from 'react';
import { createRoot } from 'react-dom/client';
import { ChatWorkspace } from '../src/components/ChatWorkspace';
import '@kingsoftcloud/ksadk-web/styles';
createRoot(document.getElementById('root')!).render(<ChatWorkspace agentId="r85-agent" agentName="R85 Network" requestedSessionId="s" />);
