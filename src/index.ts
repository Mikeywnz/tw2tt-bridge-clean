import { Hono } from 'hono'
import { serve } from '@hono/node-server'

const app = new Hono()

app.post('/webhook', async (c) => {
  let rawBody = await c.req.text()
  let body

  try {
    body = JSON.parse(rawBody)
  } catch (err) {
    body = { message: rawBody }
  }

  console.log('✅ Webhook received:', body)
  return c.json({ success: true })
})

serve(app)