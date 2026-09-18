package demo;

import org.junit.Test;
import static org.junit.Assert.assertEquals;

public class GreetingTest {
    @Test
    public void greetsNormalName() {
        assertEquals("Hello Alice", new Greeting().greet("Alice"));
    }
}
