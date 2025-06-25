import { Hono } from 'hono'

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

export default app