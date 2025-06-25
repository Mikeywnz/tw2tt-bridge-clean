import { Hono } from 'hono'
import { cors } from 'hono/cors'
import { serve } from '@hono/node-server'
import 'dotenv/config'

const app = new Hono()

app.use('*', cors())

app.post('/webhook', async (c) => {
  const body = await c.req.json()
  const authHeader = c.req.header('Authorization')

  // Validate secret
  const expectedSecret = process.env.WEBHOOK_SECRET;
  if (!expectedSecret || authHeader !== `Bearer ${expectedSecret}`) {
    console.log('❌ Unauthorized webhook attempt')
    return c.json({ success: false, error: 'Unauthorized' }, 401)
  }

  console.log('🚀 Webhook received:', body)
  return c.json({ success: true })
})

serve({
  fetch: app.fetch,
  port: 3000,
})

console.log('🚀 Webhook server running on http://localhost:3000/webhook')