import { Hono } from 'hono'
import { serve } from '@hono/node-server'
import { exec } from 'child_process'

const app = new Hono()

app.post('/webhook', async (c) => {
  const rawBody = await c.req.text()
  let body

  try {
    body = JSON.parse(rawBody)
  } catch (err) {
    body = { message: rawBody }
  }

  console.log('✅ Webhook received:', body)

  const alert = body.alert
let ticker, side, quantity

if (alert) {
  const parts = alert.split(" ")
  ticker = parts[0]
  side = parts[1]?.toLowerCase()
  quantity = parseInt(parts[2]) || 1

  const command = `python3 execute_trade_live.py ${ticker} ${side} ${quantity}`
    exec(command, (error, stdout, stderr) => {
      if (error) {
        console.error('❌ Error executing trade:', error)
      } else {
        console.log('✅ Trade executed:', stdout)
      }
    })
  } else {
    console.warn('⚠️ Incomplete alert payload – skipping trade execution')
  }

  return c.json({ success: true })
})

serve(app)