import { Hono } from 'hono'
import { cors } from 'hono/cors'
import { serve } from '@hono/node-server'

const app = new Hono()

app.use('*', cors())

app.post('/webhook', async (c) => {
  const body = await c.req.json()
  console.log('🚀 Webhook received:', body)
  return c.json({ success: true })
})

serve({
  fetch: app.fetch,
  port: 3000,
})

console.log('🚀 Webhook server running on http://localhost:3000/webhook')